#!/usr/bin/env python3
"""Tests of the AI OCR-correction suggestions (OCR review).

Usage:  python tests/test_ai.py
Parts A–D need neither Tesseract nor Ollama (they use a fake model). Part E uses the real Tesseract (English)
and F uses Playwright (skipped if missing). The "model" is always tests/fake_ollama.py: an oracle that fixes exactly
the errors the OCR made (plus traps the validations must block).
"""
from __future__ import annotations

import contextlib
import difflib
import json
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import samples  # noqa: E402
from pharos.ocr import ai_suggestions as ia_mod  # noqa: E402
from pharos.ocr.ai_suggestions import AIError, AISuggestions  # noqa: E402
from pharos.ollama import Ollama, OllamaError  # noqa: E402
from pharos.ocr import server  # noqa: E402

failures = 0


def ok(cond: bool, text: str) -> None:
    global failures
    print(("  ok    " if cond else "  FAIL  ") + text)
    if not cond:
        failures += 1


def word(txt: str, conf: float, x: int = 10) -> list:
    return [1, 1, 1, x, 10, 40, 12, conf, txt]


class OllamaStub:
    """Imitates the Ollama client without a network. `reply(part) -> str`."""

    def __init__(self, reply=lambda part: part, models=("gemma3:12b", "translategemma:12b"), delay: float = 0.0):
        self.reply, self._models, self.delay = reply, list(models), delay
        self.calls: list[dict] = []
        self.unloaded: list[str] = []

    def models(self):
        return list(self._models)

    def has_model(self, name):
        return name in self._models or (name + ":latest") in self._models

    def chat(self, model, messages, **kw):
        if self.delay:
            time.sleep(self.delay)
        user = messages[-1]["content"]
        m = re.search(r"<text>\n(.*)\n</text>", user, re.S)
        part = m.group(1) if m else user
        self.calls.append({"model": model, "system": messages[0]["content"], "user": user, "part": part})
        r = self.reply(part)
        if isinstance(r, Exception):
            raise r
        return r

    def unload(self, model):
        self.unloaded.append(model)


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="pharos_ai_"))
    print(f"Test folder: {tmp}\n")

    # ------------------------------------------------------------------ A. validation of the changes
    print("A. what the model changes: word-by-word comparison and validations")
    cmp = ia_mod.compare

    r, d = cmp("The lighthonse stood by the sea.", "The lighthouse stood by the sea.", {"lighthonse": 40.0})
    ok(len(r) == 1 and r[0]["from"] == "lighthonse" and r[0]["to"] == "lighthouse", "an OCR fix on a low-confidence word passes")
    ok("The lighthonse stood by the sea."[r[0]["start"]:r[0]["end"]] == "lighthonse", "the positions point to the original text")
    r, d = cmp("The house stood by the sea.", "The horse stood by the sea.", {"house": 96.0})
    ok(r == [] and d.get("confident_ocr") == 1, "a word the OCR read with high confidence cannot be changed")
    r, d = cmp("Das Hans stand am Meer.", "Das Haus stand am Meer.", {})
    ok(len(r) == 1, "a word not in the OCR readings (e.g. typed by you) is not blocked by confidence")
    r, d = cmp("Das Hans stand 1888 da.", "Das Haus stand 1889 da.", {"Hans": 55.0})
    ok([x["from"] for x in r] == ["Hans"] and d.get("number") == 1, "a number change is discarded; the word one proceeds")
    r, d = cmp("Die Thür und seyn Haus.", "Die Tür und sein Haus.", {"Thür": 50.0, "seyn": 50.0})
    ok(r == [] and d.get("old_spelling") == 2, "modernising old spelling (Thür→Tür, seyn→sein) is discarded")
    r, d = cmp("Die Thur und das Haus.", "Die Thür und das Haus.", {"Thur": 50.0})
    ok(len(r) == 1 and r[0]["to"] == "Thür", "a diacritic lost by the OCR (Thur→Thür) is a fix, not modernisation")
    r, d = cmp("Das Haus stand am Meer und sah hinaus.", "Das alte Haus stand am Meer und sah hinaus.", {})
    ok(r == [] and d.get("addition") == 1, "adding a word is discarded")
    r, d = cmp("Das Haus. Dann ging er.", "Das Haus, Dann ging er.", {"Haus": 40.0})
    ok(r == [] and d.get("punctuation", 0) >= 1, "a punctuation change is the author's choice and is discarded")
    r, d = cmp("Das | Haus stand da.", "Das Haus stand da.", {})
    ok(len(r) == 1 and r[0]["from"] == "|" and r[0]["to"] == "", "a symbol the OCR invented may be deleted")
    r, d = cmp("Das alte Haus stand am Meer und sah hinaus.", "Das Haus stand am Meer und sah hinaus.", {"alte": 40.0})
    ok(r == [] and d.get("removal") == 1, "deleting a real word is discarded")
    r, d = cmp("The house stood by the sea.", "The dog stood by the sea.", {"house": 40.0})
    ok(r == [] and d.get("too_different") == 1, "swapping for a very different word is discarded")
    trans = "O vento chegou de manhã cedo e trouxe consigo o cheiro do sal e da madeira úmida, e ele não disse nada."
    r, d = cmp("The wind came early in the morning and brought with it the smell of salt and damp wood, and he said nothing.", trans, {})
    ok(r == [] and d.get("rewrite") == 1, "a translated/rewritten answer is discarded entirely")
    r, d = cmp("Zeile eins hier\nZeile zwei dort\nZeile drei da", "Zeile eins hier\nZeile zwei dort\nZeile drei da", {})
    ok(r == [], "identical text produces no suggestion")
    verse = "Die Wellen schlagen\nan den Strnd hinauf\nund fliehen wieder"
    r, d = cmp(verse, verse.replace("Strnd", "Strand"), {"Strnd": 35.0})
    ok(len(r) == 1 and verse[r[0]["start"]:r[0]["end"]] == "Strnd", "in verse, the line breaks stay and the position is right")
    r, d = cmp("Er sah rn eine Tür im Dunkeln stehen.", "Er sah meine Tür im Dunkeln stehen.", {"rn": 30.0, "eine": 90.0})
    ok(len(r) == 1 and r[0]["from"] == "rn eine" and r[0]["to"] == "meine", "a word split by the OCR is rejoined (rn eine → meine)")
    r, d = cmp("Der Lenchtturm der Küste stand da.", "Der Leuchtturm den Küste stand da.", {"Lenchtturm": 40.0, "der": 97.0})
    ok([x["from"] for x in r] == ["Lenchtturm"], "in a paired change, only the word the OCR doubted counts")
    r, d = cmp("Eins zwei drei vier fünf sechs sieben acht neun zehn elf zwölf", "Un deux trois quatre cinq six sept huit neuf dix onze douze", {})
    ok(r == [] and d, "a change to almost every word: discarded")
    r, d = cmp("Der Lenchtturm\nstand am Meer", "Der Leuchtturm stand am Meer", {"Lenchtturm": 40.0})
    ok(all("\n" not in x["from"] for x in r), "a change never crosses a line break")

    ok(ia_mod.clean_response("```\nDas Haus.\n```", "Das Haus.") == "Das Haus.", "strips a code fence from the answer")
    ok(ia_mod.clean_response("\"Das Haus stand da.\"", "Das Haus stand da.") == "Das Haus stand da.", "strips quotes added by the model")
    ok(ia_mod.clean_response("Here is the corrected text:\nDas Haus stand da.", "Das Haus stand da.") == "Das Haus stand da.",
       "strips the model's intro sentence")
    ok(ia_mod.clean_response("<text>\nDas Haus.\n</text>", "Das Haus.") == "Das Haus.", "strips the returned <text> markers")
    ok(ia_mod.clean_response("\"Ja\", sagte er.", "\"Ja\", sagte er.") == "\"Ja\", sagte er.", "the text's own quotes stay")

    long_ = " ".join(f"Satz Nummer {i} steht hier und ist lang genug." for i in range(120))
    parts = ia_mod.split_into_parts(long_)
    ok(len(parts) >= 3 and "".join(p for _, p in parts) == long_ and all(len(p) <= ia_mod.MAX_PART for _, p in parts),
       f"a long block is split into {len(parts)} parts that rebuild the text")
    ok(all(long_[o:o + len(p)] == p for o, p in parts) and all(p.rstrip().endswith(".") for _, p in parts[:-1]),
       "the parts end at a sentence boundary and the offsets are right")

    ok(ia_mod.choose_model([], "my:7b", "translategemma:12b") == ("my:7b", ""), "a model chosen by the person is respected")
    ok(ia_mod.choose_model([], "", "gemma3:12b") == ("gemma3:12b", ""), "a general model from the config is used as is")
    m, av = ia_mod.choose_model(["translategemma:12b", "gemma3:1b", "gemma3:12b", "qwen3:8b"], "", "translategemma:12b")
    ok(m == "gemma3:12b" and not av, f"with a translation model, picks an installed general one: the largest that fits ({m})")
    m, av = ia_mod.choose_model(["translategemma:12b"], "", "translategemma:12b")
    ok(m == "translategemma:12b" and "ollama pull" in av, "only a translation model: uses it, but warns how to install a general one")
    try:
        ia_mod.choose_model([], "", "")
        ok(False, "no model at all: a clear error")
    except AIError as e:
        ok("--ocr-model" in str(e), "no model at all: a clear error")

    confs = ia_mod.confidence_map([word("Haus,", 40), word("Haus", 95), word("stand", 99), word("-", -1)])
    ok(confs == {"Haus": 40, "stand": 99}, "the confidence map keeps the lowest reading of each word")
    ok(ia_mod.block_suspects("Das Haus stand da", confs, 85) == ["Haus"], "only low-confidence words go as suspects")
    ok(ia_mod.block_suspects("Das Haus stand da", {"Haus": 99.0}, 85) == [], "a block with no doubtful word does not go to the model")

    # ------------------------------------------------------------------ B. generation, cache and state
    print("\nB. per-page generation, cache, reject/accept and batch pass")
    blocks = [
        {"kind": "paragraph", "text": "Der Lenchtturm stand allein am Rand der Klippe und sah aufs Meer hinaus.", "level": 1},
        {"kind": "paragraph", "text": "Dieser Absatz ist sauber gelesen worden und braucht nichts mehr.", "level": 1},
        {"kind": "title", "text": "Das Meer", "level": 1},
    ]
    words = ([word("Lenchtturm", 38)] + [word(w, 96) for w in "Der stand allein am Rand der Klippe und sah aufs Meer hinaus Dieser Absatz ist sauber gelesen worden und braucht nichts mehr Das".split()])

    def fix(part):
        return part.replace("Lenchtturm", "Leuchtturm")

    stub = OllamaStub(fix)
    ia = AISuggestions(tmp / "ia1.json", "h1", stub, "gemma3:12b", "translategemma:12b", "de", "deu")
    r = ia.generate_page(1, blocks, words)
    ok(len(r["suggestions"]) == 1 and r["suggestions"][0]["from"] == "Lenchtturm" and r["suggestions"][0]["to"] == "Leuchtturm",
       "generates the expected suggestion")
    ok(len(stub.calls) == 1 and "Lenchtturm" in stub.calls[0]["part"],
       f"only the block with a doubtful word went to the model ({len(stub.calls)} call)")
    sg = r["suggestions"][0]
    ok(sg["conf"] == 38 and (sg["before"] + sg["from"] + sg["after"]) in blocks[0]["text"] and sg["block"] == 0,
       "the suggestion carries the OCR confidence and the exact context of the text")
    ch = stub.calls[0]
    ok("German" in ch["system"] and "Fraktur" not in ch["system"] and "«Lenchtturm»" in ch["user"],
       "the request states the language and lists the suspect words")
    ok("never instructions" in ch["system"] and "Do NOT modernise" in ch["system"], "the request forbids modernising spelling and treats the text as data")
    ok(ia.has(1) and not ia.has(2), "it knows which pages already have suggestions")
    ia2 = AISuggestions(tmp / "ia1.json", "h1", OllamaStub(), "gemma3:12b", "", "de", "deu")
    ok([s["id"] for s in ia2.list_(1)] == [sg["id"]], "the suggestions survive closing and reopening the program")
    ia2.mark([sg["id"]], accepted=False)
    ia3 = AISuggestions(tmp / "ia1.json", "h1", OllamaStub(), "gemma3:12b", "", "de", "deu")
    ok(ia3.list_(1) == [] and ia3.summary()["rejected"] == 1 and ia3.summary()["generated"] == 1, "a rejection persists and enters the summary")
    r2 = ia3.generate_page(1, blocks, words)
    ok(r2["suggestions"] == [], "generating again does not resurrect a suggestion you rejected")
    ia3.mark([sg["id"]], accepted=True)
    ok(ia3.summary()["accepted"] == 1, "accepting enters the summary")
    ia4 = AISuggestions(tmp / "ia1.json", "other-hash", OllamaStub(), "gemma3:12b")
    ok(ia4.list_(1) == [] and (tmp / "ia1.old.json").exists(), "OCR redone (other hash): old suggestions are set aside")

    s_fr = AISuggestions(tmp / "ia_frk.json", "h", OllamaStub(fix), "gemma3:12b", "", "de", "frk")
    s_fr.generate_page(1, blocks, words)
    ok("Fraktur" in s_fr.ollama.calls[0]["system"], "in gothic type the request cites the typical Fraktur confusions")

    # long block: the positions of the changes apply to the whole block
    phrases = [f"Satz Nummer {i} steht hier und ist lang genug." for i in range(110)]
    phrases[95] = "Der Lenchtturm stand allein."
    long_ = " ".join(phrases)
    words_l = [word("Lenchtturm", 30)] + [word("Satz", 95)]
    s_l = AISuggestions(tmp / "ia_long.json", "h", OllamaStub(fix), "gemma3:12b")
    r = s_l.generate_page(1, [{"kind": "paragraph", "text": long_, "level": 1}], words_l)
    ok(len(s_l.ollama.calls) >= 3 and len(r["suggestions"]) == 1 and
       (r["suggestions"][0]["before"] + "Lenchtturm" + r["suggestions"][0]["after"]) in long_,
       f"a long block goes in parts ({len(s_l.ollama.calls)}) and the suggestion lands in the right place")

    # problematic model answers
    for name, resp in (("translate", lambda p: "O vento chegou de manhã cedo e trouxe consigo o cheiro do sal. " * 2),
                       ("with preamble", lambda p: "Here is the corrected text:\n" + fix(p)),
                       ("in a code fence", lambda p: "```\n" + fix(p) + "\n```")):
        s_x = AISuggestions(tmp / f"ia_{abs(hash(name))}.json", "h", OllamaStub(resp), "gemma3:12b")
        rx = s_x.generate_page(1, blocks, words)
        expected = 0 if name == "translate" else 1
        ok(len(rx["suggestions"]) == expected, f"answer '{name}': {'nothing is suggested' if not expected else 'the real fix still holds'}")

    # failures
    s_e = AISuggestions(tmp / "ia_err.json", "h", OllamaStub(lambda p: OllamaError("dropped")), "gemma3:12b")
    try:
        s_e.generate_page(1, blocks, words)
        ok(False, "an Ollama error is raised")
    except OllamaError:
        ok(not (tmp / "ia_err.json").exists() and not s_e.has(1), "Ollama error: nothing is saved and the error reaches the caller")
    s_m = AISuggestions(tmp / "ia_no.json", "h", OllamaStub(models=("other:7b",)), "gemma3:12b")
    try:
        s_m.generate_page(1, blocks, words)
        ok(False, "a missing model raises an error")
    except AIError as e:
        ok("ollama pull gemma3:12b" in str(e), "model not installed: the message gives the command to install it")
    est = s_m.state()
    ok(est["available"] is False and "gemma3:12b" in est["reason"], "state reports that it is unavailable and why")

    # batch pass
    words_p = {n: words for n in range(1, 6)}
    blocks_p = {n: blocks for n in range(1, 6)}
    source = lambda n: (blocks_p[n], words_p[n])  # noqa: E731
    s_b = AISuggestions(tmp / "ia_batch.json", "h", OllamaStub(fix, delay=0.05), "gemma3:12b")
    s_b.start_batch([1, 2, 3, 4, 5], source, "all")
    try:
        s_b.start_batch([1], source)
        ok(False, "two passes at the same time are refused")
    except AIError:
        ok(True, "two passes at the same time are refused")
    s_b.wait_(20)
    t = s_b.work_state()
    ok(not t["active"] and t["done"] == 5 and t["suggestions"] == 5 and not t["error"], f"batch finishes: {t['done']} pages, {t['suggestions']} suggestions")
    ok(all(s_b.has(n) for n in range(1, 6)), "each page is stored")

    s_p = AISuggestions(tmp / "ia_stop.json", "h", OllamaStub(fix, delay=0.3), "gemma3:12b")
    s_p.start_batch([1, 2, 3, 4, 5], source, "all")
    time.sleep(0.45)
    s_p.stop_()
    s_p.wait_(20)
    t = s_p.work_state()
    ok(not t["active"] and 0 < t["done"] < 5, f"stop interrupts after the current page ({t['done']} of 5 done)")

    s_f = AISuggestions(tmp / "ia_fail.json", "h", OllamaStub(lambda p: OllamaError("dropped")), "gemma3:12b")
    s_f.start_batch([1, 2, 3, 4], source)
    s_f.wait_(30)
    t = s_f.work_state()
    ok(not t["active"] and t["done"] == 2 and "dropped" in t["error"], f"two failures in a row end the batch and record the error ({t['done']} tried)")

    s_d = AISuggestions(tmp / "ia_unload.json", "h", stub, "gemma3:12b")
    s_d.unload()
    ok(stub.unloaded == [], "does not unload the model if it was never used")
    stub.unloaded.clear()
    s_u = AISuggestions(tmp / "ia_unload2.json", "h", stub, "gemma3:12b")
    s_u.generate_page(1, blocks, words)
    s_u.unload()
    ok(stub.unloaded == ["gemma3:12b"], "after using it, unloads the model to free memory")

    # ------------------------------------------------------------------ C. the session: never applies on its own
    print("\nC. review session: the suggestions do not touch the text")
    from pharos.ocr.review import Review, Session

    def make_session(stub_ai, name):
        pages = {n: {"n": n, "width": 1000, "height": 1400, "words": words, "hash": "h"} for n in (1, 2, 3, 4)}
        auto = {n: [dict(b) for b in blocks] for n in pages}
        scores = {1: {"score": 60, "flagged": True, "words": 40}, 2: {"score": 95, "flagged": False, "words": 40},
                  3: {"score": 70, "flagged": True, "words": 40}, 4: {"score": 99, "flagged": False, "words": 2}}
        rev = Review(tmp / f"rev_{name}.json", "h")
        ia_ = AISuggestions(tmp / f"{name}.json", "h", stub_ai, "gemma3:12b", "", "de", "deu")
        return Session("Test", pages, auto, scores, [], rev, tmp, ai=ia_), ia_

    ses, ia_s = make_session(OllamaStub(fix), "ses1")
    before_blocks = ses.final_blocks()
    ses.ai_generate_page(1)
    ses.ai_start_batch("all")
    ia_s.wait_(20)
    ok(ses.final_blocks() == before_blocks and not ses.review.edited_pages(),
       "after generating suggestions (page and batch), the text and the edits stay exactly the same")
    ok(len(ses.ai_for_page(1)) == 1 and ses.page(1)["ai"][0]["to"] == "Leuchtturm", "the page carries the suggestions separately, alongside the text")
    idx = {p["n"]: p for p in ses.index()["pages"]}
    ok(idx[1]["ai"] == 1 and idx[3]["ai"] == 1 and idx[4]["ai"] == 0 and ses.index()["ai"] is True, "the index shows how many suggestions each page has")
    ses.save(1, [{"kind": "paragraph", "text": "Der Leuchtturm stand allein.", "level": 1}], True)
    ok(ses.ai_for_page(1) == [], "if you already fixed the text by hand, the suggestion stops showing")
    ses2, ia_s2 = make_session(OllamaStub(fix), "ses2")
    ses2.mark(2, True)
    ia_s2.generate_page(3, *ses2._ai_source(3))
    ok(ses2.pages_for_ai("flagged") == [1], "batch 'flagged' skips reviewed, already generated and text-less pages")
    ok(ses2.pages_for_ai("all") == [1], "batch 'all' skips reviewed, already generated and nearly text-less pages")
    no_ai = Session("x", {1: {"n": 1, "width": 1, "height": 1, "words": []}}, {}, {}, [], Review(tmp / "rev_x.json", "h"), tmp)
    ok(no_ai.ai_for_page(1) == [] and no_ai.ai_state()["available"] is False and no_ai.index()["ai"] is False,
       "with no AI configured, nothing changes in the session")

    # ------------------------------------------------------------------ D. HTTP server with a real (fake) Ollama
    print("\nD. review server + fake Ollama over HTTP")
    from test_flow import Fake

    def call_(srv, path, body=None, token=True):
        base = srv.url.split("/?")[0]
        req = urllib.request.Request(base + path, data=None if body is None else json.dumps(body).encode(),
                                     headers={"X-Token": srv.token} if token else {})
        if body is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def http_session(ollama_url, name, model="gemma3:12b"):
        ses_, ia_ = make_session(Ollama(ollama_url, timeout=30), name)
        ia_.preferred = model
        return ses_, ia_

    with Fake(tmp, FAKE_OCR_SWAPS="Lenchtturm=Leuchtturm") as f:
        ses_h, ia_h = http_session(f.url, "http1")
        srv = server.start_in_thread(ses_h)
        try:
            ok(call_(srv, "/api/ai", token=False)[0] == 403, "without a token, the AI API refuses")
            c, est = call_(srv, "/api/ai")
            ok(c == 200 and est["available"] and est["model"] == "gemma3:12b" and not est["work"]["active"], "state: available, right model")
            c, r = call_(srv, "/api/ai/page/1", {})
            ok(c == 200 and len(r["suggestions"]) == 1 and r["suggestions"][0]["to"] == "Leuchtturm", "POST generates the page's suggestion")
            ok(call_(srv, "/api/page/1")[1]["blocks"][0]["text"].count("Lenchtturm") == 1, "the page text stays intact")
            ident = r["suggestions"][0]["id"]
            c, _ = call_(srv, "/api/ai/reject", {"ids": [ident]})
            ok(c == 200 and call_(srv, "/api/ai/page/1")[1]["suggestions"] == [], "rejecting hides the suggestion")
            ok(call_(srv, "/api/ai/accept", {"ids": "x"})[0] == 400, "invalid ids: 400")
            ok(call_(srv, "/api/ai/page/99", {})[0] == 409 and call_(srv, "/api/ai/page/99")[0] == 404, "a nonexistent page is refused")
            c, r = call_(srv, "/api/ai/batch", {"scope": "flagged"})
            ok(c == 200 and r.get("total") == 1, f"batch 'flagged' starts (answer: {r.get('total')} pending page)")
            deadline = time.time() + 30
            while time.time() < deadline and call_(srv, "/api/ai")[1]["work"]["active"]:
                time.sleep(0.2)
            t = call_(srv, "/api/ai")[1]["work"]
            ok(not t["active"] and t["done"] == 1 and call_(srv, "/api/ai/page/3")[1]["suggestions"], "batch finishes and page 3 gets a suggestion")
            ok(call_(srv, "/api/ai/batch", {"scope": "x"})[0] == 409, "invalid scope: a clear error")
            c, r = call_(srv, "/api/ai/batch", {"scope": "flagged"})
            ok(c == 200 and r.get("nothing") is True, "a batch with no pending pages says there is nothing to do")
        finally:
            srv.server_close()
        reqs = [x for x in f.requests if "proofreader" in x["messages"][0]["content"]]
        ok(reqs and reqs[0]["model"] == "gemma3:12b", f"the Ollama calls used the chosen model ({len(reqs)} calls)")

    with Fake(tmp, FAKE_OCR_MODE="error") as f:
        ses_h, ia_h = http_session(f.url, "http2")
        srv = server.start_in_thread(ses_h)
        try:
            c, r = call_(srv, "/api/ai/page/1", {})
            ok(c == 502 and "error" in r, f"Ollama erroring: the screen gets a 502 and the message ({c})")
        finally:
            srv.server_close()
    with Fake(tmp, FAKE_OCR_MODE="translate") as f:
        ses_h, ia_h = http_session(f.url, "http3")
        srv = server.start_in_thread(ses_h)
        try:
            c, r = call_(srv, "/api/ai/page/1", {})
            ok(c == 200 and r["suggestions"] == [] and r["info"]["discards"].get("rewrite"), "a model that translates: no suggestion, and the discard is recorded")
        finally:
            srv.server_close()
    ses_h, ia_h = http_session("http://127.0.0.1:9", "http4")
    srv = server.start_in_thread(ses_h)
    try:
        c, est = call_(srv, "/api/ai")
        ok(c == 200 and est["available"] is False and "Ollama" in est["reason"], "Ollama down: the screen is warned without hanging")
        c, r = call_(srv, "/api/ai/page/1", {})
        ok(c == 409 and "Ollama" in r["error"], "and asking for a suggestion gives a clear message (409)")
    finally:
        srv.server_close()

    if "--units-only" in sys.argv:
        print(f"\n{'ALL GOOD' if not failures else str(failures) + ' check(s) failed'} (A–D only) — {tmp}")
        return 1 if failures else 0

    return ocr_part(tmp, ok_final=lambda: failures)


def real_swaps(texts: list[str], lexicon: dict[str, str]) -> dict[str, str]:
    """The fake model's "oracle": for each read word that does not exist in the true book, the most similar true word."""
    table = {}
    keys = list(lexicon)
    for t in set(re.findall(r"\w+", " ".join(texts))):
        if len(t) < 4 or t.lower() in lexicon:
            continue
        match = difflib.get_close_matches(t.lower(), keys, n=1, cutoff=0.75)
        if match:
            right = lexicon[match[0]]
            table[t] = right[0].upper() + right[1:] if t[0].isupper() else right
    return table


def ocr_part(tmp: Path, ok_final) -> int:
    from pharos.ocr import layout, engine, quality, tesseract
    from pharos.ocr.review import Review, Session
    from test_flow import Fake
    from test_ocr import api, paragraphs, run_interactive

    def finish_(msg: str = "") -> int:
        print(f"\n{'ALL GOOD' if not failures else str(failures) + ' check(s) failed'}{msg} — files in {tmp}")
        return 1 if failures else 0

    try:
        exe = tesseract.locate()
        installed = tesseract.installed_languages(exe)
    except Exception as e:  # noqa: BLE001
        print(f"\nTesseract not found ({e}); parts E and F skipped.")
        return finish_(" (A–D)")
    if "eng" not in installed:
        print("\nTesseract does not have the 'eng' language; parts E and F skipped.")
        return finish_(" (A–D)")

    # ------------------------------------------------------------------ E. end-to-end: bad scan + real OCR + CLI
    print("\nE. end-to-end: degraded scan, real OCR, review with AI suggestions")
    sm = tmp / "samples"
    sm.mkdir()
    samples.make_pdf(sm / "lighthouse.pdf", "indent", "en")
    expected = samples.expected_text_en()
    lexicon = {w.lower(): w.lower() for w in re.findall(r"\w+", " ".join(expected))}
    folder = tmp / "bad_scan"
    folder.mkdir()
    for i, im in enumerate(samples.render_pdf(sm / "lighthouse.pdf", 200), 1):
        d = samples.degrade(im, 1.0, 60.0, blur=2.2, seed=i) if i in (3, 4) else samples.degrade(im, 1.0, 5.0, seed=i)
        d.save(folder / f"page_{i:03d}.jpg", quality=75)
    swaps_file = tmp / "swaps.json"
    proj = tmp / "e" / "bad-scan-book"
    seen: dict = {}

    def session_for(srv_url, path):
        ns = sorted(int(p.stem[1:]) for p in (proj / "ocr").glob("p*.json"))
        pages = engine.load_pages(proj / "ocr", ns)
        auto_b, _ = layout.analyze_document(pages)
        scores = quality.score_all(pages)
        ia_ = AISuggestions(path, "hash-test", Ollama(srv_url, timeout=120), "gemma3:12b", "", "en", "eng")
        ses_ = Session("AI test", pages, auto_b, scores, [], Review(tmp / f"rev_{path.stem}.json", "hash-test"),
                       proj / "pages", initial_filter="all", ai=ia_)
        return ses_, ia_, pages, auto_b

    with Fake(tmp, FAKE_OCR_SWAPS_FILE=str(swaps_file)) as fake:
        args1 = [str(folder), "--output", str(tmp / "e"), "--source", "en", "--target", "pt", "--no-browser", "--no-preview",
                 "--export-only", "--ollama", fake.url, "--ocr-model", "gemma3:12b", "--review", "flagged"]

        def review_(url, p):
            seen["state"] = api(url, "/api/ai")
            before = api(url, "/api/page/3")["blocks"]
            texts = [b["text"] for b in before]
            pal3 = engine.load_pages(proj / "ocr", [3])[3]["words"]
            confs = ia_mod.confidence_map(pal3)
            table = real_swaps(texts, lexicon)
            eligible = " ".join(t for t in texts if ia_mod.block_suspects(t, confs))
            bait = next((w for w in re.findall(r"\w+", eligible) if len(w) >= 6 and confs.get(w, 0) >= 96 and w.lower() in lexicon), None)
            if bait:
                table[bait] = bait[:-1] + ("x" if bait[-1] != "x" else "z")
            seen["table"], seen["bait"] = table, bait
            swaps_file.write_text(json.dumps(table), encoding="utf-8")
            r = api(url, "/api/ai/page/3", {})
            seen["sug"], seen["info"] = r["suggestions"], r["info"]
            after = api(url, "/api/page/3")["blocks"]
            seen["intact"] = after == before
            blocks = after
            sugs = r["suggestions"]
            seen["accepted"] = sugs[-1] if sugs else None
            seen["rejected"] = sugs[0] if len(sugs) >= 2 else None
            if seen["rejected"]:
                api(url, "/api/ai/reject", {"ids": [seen["rejected"]["id"]]})
            if seen["accepted"]:
                sg = seen["accepted"]
                t = blocks[sg["block"]]["text"]
                i = t.find(sg["before"] + sg["from"] + sg["after"]) + len(sg["before"])
                blocks[sg["block"]]["text"] = t[:i] + sg["to"] + t[i + len(sg["from"]):]
                api(url, "/api/ai/accept", {"ids": [sg["id"]]})
            api(url, "/api/page/3", {"blocks": blocks, "reviewed": True})
            for x in api(url, "/api/index")["pages"]:
                if x["flagged"]:
                    api(url, f"/api/mark/{x['n']}", {"reviewed": True})
            api(url, "/api/finish", {})

        cod, out, url = run_interactive(args1, tmp, review_, timeout=400)
        est = seen.get("state", {})
        ok(cod == 0, f"program finished (code {cod})" + ("" if cod == 0 else "\n" + out[-1500:]))
        ok(est.get("available") and est.get("model") == "gemma3:12b", f"the screen sees the AI available with the chosen model ({est.get('model')})")
        sug = seen.get("sug", [])
        table = seen.get("table", {})
        ok(len(sug) >= 1, f"the degraded page got {len(sug)} suggestion(s) from real OCR errors")
        ok(all(table.get(s["from"]) == s["to"] for s in sug), "every suggestion is one of the fixes the model proposed (nothing extra gets in)")
        ok(seen.get("intact") is True, "generating the suggestions did not change the page text")
        if seen.get("bait"):
            ok(seen["bait"] not in [s["from"] for s in sug] and seen["info"]["discards"].get("confident_ocr", 0) >= 1,
               f"the bait (word '{seen['bait']}' read with high confidence) was blocked")
        else:
            print("  (no high-confidence bait word in the eligible blocks; check skipped)")
        ok("AI suggestions (gemma3:12b)" in out, "the program reports the suggestion summary at the end of the review")
        rep = (proj / "review" / "ocr_quality.md").read_text(encoding="utf-8")
        ok("AI correction suggestions" in rep and "nothing is applied without your decision" in rep, "the quality report records the AI suggestions")
        ai_file = proj / "review" / "ocr_ai_suggestions.json"
        ok(ai_file.exists() and json.loads(ai_file.read_text(encoding="utf-8"))["model"] == "gemma3:12b", "the suggestions are saved in the project")
        book_text = " ".join(paragraphs(proj / "extracted" / "book.json"))
        if seen.get("accepted"):
            ok(seen["accepted"]["from"] not in book_text and seen["accepted"]["to"] in book_text,
               f"the accepted suggestion ('{seen['accepted']['from']}' → '{seen['accepted']['to']}') reached the book")
        if seen.get("rejected"):
            ok(seen["rejected"]["from"] in book_text, f"the rejected suggestion ('{seen['rejected']['from']}') was NOT applied to the book")

        # second run: --ai-suggestions starts on its own, without applying anything
        seen2: dict = {}

        def batch_(url, p):
            deadline = time.time() + 90
            while time.time() < deadline:
                t = api(url, "/api/ai")["work"]
                if not t["active"] and (t["total"] or time.time() > deadline - 80):
                    break
                time.sleep(0.4)
            seen2["work"] = api(url, "/api/ai")["work"]
            seen2["again"] = api(url, "/api/ai/page/3", {})["suggestions"]
            api(url, "/api/finish", {})

        args2 = [str(proj), "--no-browser", "--no-preview", "--export-only", "--ollama", fake.url,
                 "--review-ocr", "--ai-suggestions", "all"]
        cod, out, url = run_interactive(args2, tmp, batch_, timeout=400)
        t = seen2.get("work", {})
        ok(cod == 0 and "AI: suggesting fixes on" in out, "--ai-suggestions starts the pass on its own when the review opens")
        ok(t and not t["active"] and t["done"] == t["total"] and t["total"] >= 3, f"the pass covers the pages still without a suggestion ({t.get('done')}/{t.get('total')})")
        returned = [x["from"] for x in seen2.get("again", [])]
        ok(seen["accepted"]["from"] not in returned and seen["rejected"]["from"] not in returned,
           f"asking again on page 3 does not bring back the accepted or the rejected suggestion ({len(returned)} others remain pending)")
        book_text2 = " ".join(paragraphs(proj / "extracted" / "book.json"))
        ok(book_text2 == book_text, "after the batch pass, the book text stays exactly the same (a suggestion is not an application)")
        ok(any(r for r in fake.requests if "proofreader" in r["messages"][0]["content"]), "the fake model received the review requests")

    # ------------------------------------------------------------------ F. the screen in the browser
    print("\nF. review screen with AI suggestions in the browser")
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("  (skipped: Playwright is not installed)")
        return finish_(" (A–E)")

    ns = sorted(int(p.stem[1:]) for p in (proj / "ocr").glob("p*.json"))
    pages0 = engine.load_pages(proj / "ocr", ns)
    auto0, _ = layout.analyze_document(pages0)
    table_f = real_swaps([b["text"] for n in ns for b in auto0.get(n, [])], lexicon)
    file_f = tmp / "swaps_f.json"
    file_f.write_text(json.dumps(table_f), encoding="utf-8")
    ok(len(table_f) >= 2, f"(setup) the oracle knows {len(table_f)} wrong readings of the book")

    js_errors: list[str] = []
    dialogs: list[str] = []
    chromium_exe = Path("/opt/pw-browsers/chromium")
    with Fake(tmp, FAKE_OCR_SWAPS_FILE=str(file_f), FAKE_OCR_MODE="slow", FAKE_OCR_DELAY="0.5") as fake_f:
        ses, ia_f, _, _ = session_for(fake_f.url, tmp / "ia_ui.json")
        srv = server.start_in_thread(ses)
        try:
            with sync_playwright() as pw:
                nav = pw.chromium.launch(executable_path=str(chromium_exe)) if chromium_exe.exists() else pw.chromium.launch()
                ctx = nav.new_context(viewport={"width": 1500, "height": 950})
                pg = ctx.new_page()
                pg.on("pageerror", lambda e: js_errors.append(str(e)))
                pg.on("console", lambda m: js_errors.append(m.text) if m.type == "error" else None)

                def dialog(d):
                    dialogs.append(d.message)
                    d.accept()

                pg.on("dialog", dialog)
                pg.goto(srv.url)
                pg.wait_for_selector("#blocks textarea")
                pg.wait_for_function("!document.querySelector('#ai-run').disabled", timeout=20000)
                ok(pg.locator("#ai-ctl").is_visible(), "the AI control appears when the session has AI")
                ok("gemma3:12b" in (pg.locator("#ai-run").get_attribute("title") or ""), "the button reports the model")
                pg.locator('#list .item[data-n="3"]').click()
                pg.wait_for_function("document.querySelector('#page-title').textContent.includes('Page 3')")
                ok(pg.locator("#ai-sug .item").count() == 0, "before asking, there are no suggestions on the screen")
                text_before = [t.input_value() for t in pg.locator("#blocks textarea").all()]
                pg.click("#ai-run")
                pg.wait_for_selector("#ai-sug .item", timeout=60000)
                n_sug = pg.locator("#ai-sug .item").count()
                ok(n_sug >= 1, f"the AI suggests {n_sug} change(s) on page 3 (shown with the before and the after)")
                ok(pg.locator("#ai-sug del").count() == n_sug and pg.locator("#ai-sug ins").count() == n_sug,
                   "each suggestion shows the current fragment struck through and the proposed one")
                ok([t.input_value() for t in pg.locator("#blocks textarea").all()] == text_before, "the suggestions appear without touching the text")
                pg.screenshot(path=str(tmp / "ai_light.png"))
                from0 = pg.locator("#ai-sug del").first.inner_text()
                to0 = pg.locator("#ai-sug ins").first.inner_text()
                pg.locator("#ai-sug .item").first.locator("text=accept").first.click()
                pg.wait_for_timeout(300)
                tas = " ".join(t.input_value() for t in pg.locator("#blocks textarea").all())
                ok(to0 in tas and pg.locator("#ai-sug .item").count() == n_sug - 1, f"accepting applies '{from0}' → '{to0}' in the text and removes it from the list")
                ok(ia_f.summary()["accepted"] == 1, "the server records the acceptance")
                if n_sug >= 3:
                    pg.locator("#ai-sug .item").first.locator("text=reject").first.click()
                    pg.wait_for_timeout(300)
                    ok(pg.locator("#ai-sug .item").count() == n_sug - 2 and ia_f.summary()["rejected"] == 1, "rejecting removes the suggestion without touching the text")
                remaining = pg.locator("#ai-sug .item").count()
                if remaining:
                    pg.locator("#ai-sug .item .tx").first.click()
                    ok(pg.evaluate("document.activeElement.tagName") == "TEXTAREA", "clicking the suggestion jumps to the fragment in the text")
                    pg.get_by_text("accept all").click()
                    pg.wait_for_timeout(300)
                    ok(pg.locator("#ai-sug .item").count() == 0, "'accept all' applies the ones left on this page")
                pg.keyboard.press("Control+Enter")
                pg.wait_for_timeout(700)
                saved = " ".join(b["text"] for b in ses.review.blocks(3) or [])
                ok(to0 in saved and ses.review.reviewed(3), "saving the page writes the already-corrected text")

                # batch pass from the screen: progress, Stop and continue
                pg.select_option("#ai-scope", "all")
                pg.click("#ai-run")
                pg.wait_for_function("document.querySelector('#ai-status').textContent.startsWith('AI ')", timeout=15000)
                ok(pg.locator("#ai-stop").is_visible(), "the batch pass shows the progress and the Stop button")
                pg.click("#ai-stop")
                deadline = time.time() + 60
                while time.time() < deadline and ia_f.work_state()["active"]:
                    time.sleep(0.2)
                t1 = ia_f.work_state()
                ok(not t1["active"] and t1["done"] < t1["total"], f"Stop interrupts the pass ({t1['done']} of {t1['total']} pages)")
                pg.wait_for_timeout(2600)
                pg.click("#ai-run")
                deadline = time.time() + 120
                while time.time() < deadline and (ia_f.work_state()["total"] == t1["total"] or ia_f.work_state()["active"]):
                    time.sleep(0.3)
                t2 = ia_f.work_state()
                ok(t2["total"] == t1["total"] - t1["done"] and t2["done"] == t2["total"],
                   f"running again continues only on the pages that were missing ({t2['done']}/{t2['total']})")
                pg.wait_for_timeout(2600)
                with_badge = pg.locator("#list .badge.ai").count()
                ok(with_badge >= 1, f"when it finishes, the pages with suggestions get the 'AI n' badge ({with_badge})")
                ok(pg.locator("#ai-stop").is_hidden(), "the Stop button disappears when the pass finishes")

                # visual: dark mode and mobile on a page with suggestions
                target = next((p["n"] for p in ses.index()["pages"] if p["ai"] and p["n"] != 3), None)
                if target:
                    for name, kw in (("ai_dark.png", {"viewport": {"width": 1500, "height": 950}, "color_scheme": "dark"}),
                                     ("ai_mobile.png", {"viewport": {"width": 390, "height": 844}})):
                        c2 = nav.new_context(**kw)
                        p2 = c2.new_page()
                        p2.goto(srv.url)
                        p2.wait_for_selector("#blocks textarea")
                        p2.locator(f'#list .item[data-n="{target}"]').click()
                        p2.wait_for_selector("#ai-sug .item", timeout=15000)
                        p2.screenshot(path=str(tmp / name), full_page=name == "ai_mobile.png")
                        if name == "ai_mobile.png":
                            ok(p2.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1"), "on mobile the screen gains no horizontal scroll")
                        c2.close()
                # finishing warns that pending suggestions will NOT be applied
                pg.click("#finish")
                pg.wait_for_timeout(900)
                ok(any("will NOT be applied" in d for d in dialogs), "on finishing, the screen warns that pending suggestions will not be applied")
                ok(srv.finished, "the review finishes normally")
                nav.close()
        finally:
            with contextlib.suppress(Exception):
                srv.server_close()
    ok(not js_errors, f"no errors in the browser console {js_errors[:2] if js_errors else ''}")
    return finish_()


if __name__ == "__main__":
    sys.exit(main())
