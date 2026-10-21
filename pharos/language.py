"""Language detection without external dependencies.

Latin script: scores function words of German, English, Spanish, French, Italian,
Portuguese, Dutch and Latin (which common libraries such as langdetect do not recognise).
Other writing systems: Cyrillic, Greek, Hebrew, Arabic, Hangul, Kana and Han.
For other languages the program asks the user.
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict

from .model import Book, PARAGRAPH

_WORDLISTS = {
    "de": """der die das den dem des ein eine einer einem einen eines und oder aber nicht ist sind war
        waren wird werden wurde wurden hat haben hatte kann können muss soll mit von zu zum zur auf für
        über unter durch vor nach bei aus an im am um bis wie als wenn dass daß weil auch nur noch schon
        sehr so ich du er sie es wir ihr mich dich sich uns euch mein dein sein ihre seine dieser diese
        dieses jeder kein keine man mehr dann denn doch ja nein hier dort wo was wer wann warum""",
    "en": """the and of to in is that it was for on are with as be this by have from or at not but his her
        they which an we you he she there their what all were when your can said if will one has had been
        would could should may might do does did them then than these those its our my me him us who whom
        whose into out up about over after before between through because while where how why also only
        very more most some any such no yes""",
    "es": """el la los las un una unos unas de del que y en es por con para no se su sus al lo como más pero
        muy sin sobre también hasta hay donde desde todo todos nos les ellos ellas esto eso esta este ese
        esa estos estas antes qué cuando porque entre otro otra otros otras tanto mucho nada ella él yo tú
        usted nosotros ser estar fue era son han ha había puede pueden mi tu cada así aquí allí sí""",
    "pt": """o a os as de do da dos das em no na nos nas um uma uns umas que e é são foi era ser tem têm há
        com sem para por pelo pela pelos pelas ao aos não mas ou se como mais muito também já seu sua seus
        suas ele ela eles elas isso isto esse essa este esta estes estas quando entre até sobre então
        mesmo depois só você nós eu me te lhe lhes meu minha cada assim aqui ali sim onde porque pode
        podem foram tinha""",
    "fr": """le la les un une des du de au aux et est sont était été être avoir a ont avait que qui quoi
        dont où dans pour par sur avec sans sous ce cette ces cet il elle ils elles on nous vous je tu me
        te se lui leur leurs son sa ses mon ma mes ton ta tes mais ou donc car ne pas plus très aussi
        tout tous toute toutes comme si fait peut peuvent entre depuis chez vers""",
    "it": """il lo la i gli le un uno una dei delle del della dello degli di da in con su per tra fra e è
        sono era erano essere ha hanno aveva che chi cui non si come più ma anche questo questa questi
        queste quello quella quelli quelle suo sua suoi sue loro lui lei noi voi io mi ti ci vi ne molto
        tutto tutti tutte quando dove perché così già ancora solo sempre nel nella nei nelle sul sulla
        al alla ai alle""",
    "nl": """de het een en van in is dat op te zijn met voor niet aan er maar om ook als dan ze hij zij wij
        ik je u wordt worden werd werden naar door over bij uit nog wel dit deze die zo hun haar mijn onze
        heeft hebben had waren was kan kunnen moet zal zou al zeer meer veel geen toen hier daar waar wat
        wie hoe waarom omdat tot onder tussen""",
    "la": """et in est non ad cum ut quod quae qui quo quem quam quid sed per ex de ab sunt esse hoc haec hic
        ille illa illud id eius eorum sic si nec aut enim autem quia ergo omnia omnis omne sit erat erant
        fuit cui quibus quos quas atque neque vel etiam tamen ita sicut apud inter sub super post ante
        contra""",
}

_USAGE: dict[str, list[str]] = defaultdict(list)
for _lang, _text in _WORDLISTS.items():
    for _w in set(_text.split()):
        _USAGE[_w].append(_lang)

_RE_WORD = re.compile(r"[^\W\d_]+", re.UNICODE)


def _script(text: str) -> Counter:
    c: Counter = Counter()
    for ch in text:
        o = ord(ch)
        if 0x0400 <= o <= 0x04FF:
            c["ru"] += 1
        elif 0x0370 <= o <= 0x03FF or 0x1F00 <= o <= 0x1FFF:
            c["el"] += 1
        elif 0x0590 <= o <= 0x05FF:
            c["he"] += 1
        elif 0x0600 <= o <= 0x06FF:
            c["ar"] += 1
        elif 0xAC00 <= o <= 0xD7AF or 0x1100 <= o <= 0x11FF:
            c["ko"] += 1
        elif 0x3040 <= o <= 0x30FF:
            c["ja"] += 1
        elif 0x4E00 <= o <= 0x9FFF:
            c["han"] += 1
        elif ch.isalpha():
            c["latin"] += 1
    return c


def detect_snippet(text: str, min_letters: int = 40) -> tuple[str, float] | None:
    """Likely language of a snippet: (code, share of the points). None if too short or undetermined."""
    sc = _script(text)
    total = sum(sc.values())
    if total < min_letters:
        return None
    for key in ("ru", "el", "he", "ar", "ko"):
        if sc[key] / total > 0.5:
            return key, sc[key] / total
    if sc["ja"] / total > 0.1:
        return "ja", 1.0
    if sc["han"] / total > 0.5:
        return "zh-cn", 1.0
    if sc["latin"] / total < 0.5:
        return None

    scores: Counter = Counter()
    for word in _RE_WORD.findall(text.lower()):
        langs = _USAGE.get(word)
        if langs:
            weight = 1.0 / len(langs)
            for l in langs:
                scores[l] += weight
    total_score = sum(scores.values())
    if total_score < 1.5:
        return None
    lang, top = scores.most_common(1)[0]
    return lang, top / total_score


def detect_book(book: Book, max_samples: int = 60) -> list[tuple[str, float]]:
    """Samples paragraphs scattered through the book; returns [(code, fraction)] in descending order."""
    candidates = [b for b in book.blocks() if b.kind == PARAGRAPH and len(b.text) >= 120]
    if not candidates:
        candidates = [b for b in book.blocks() if len(b.text) >= 40]
    if not candidates:
        return []
    step = max(1, len(candidates) // max_samples)
    weights: Counter = Counter()
    for b in candidates[::step][:max_samples]:
        r = detect_snippet(b.text[:1500])
        if r:
            weights[r[0]] += len(b.text)
    total = sum(weights.values())
    if not total:
        return []
    return sorted(((l, p / total) for l, p in weights.items()), key=lambda x: -x[1])
