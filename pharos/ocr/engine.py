"""Runs OCR across pages: in parallel, with a per-page cache (resumable) and images for the review."""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

from . import preprocess, tesseract
from .preprocess import prepare as prepare_page
from .pages import PageSource
from .tesseract import OCRError

VERSION = 1  # bump when the pre-processing changes (invalidates the cache)
REORIENT_THRESHOLD = 55  # mean confidence below this -> try to detect a rotated page


@dataclass
class OCRParams:
    lang: str
    psm: int = 3
    dpi: int = 300

    def hash(self) -> str:
        return hashlib.sha1(f"{VERSION}|{self.lang}|{self.psm}|{self.dpi}".encode()).hexdigest()[:10]


def page_plan(source: PageSource, indices: list[int], doubles: bool) -> list[dict]:
    """Logical pages to process. With doubles, a wide image becomes two pages (left, right)."""
    plan: list[dict] = []
    n = 0
    for i in indices:
        if doubles and preprocess.is_double(*source.size(i)):
            for half in (0, 1):
                n += 1
                plan.append({"n": n, "source": i, "half": half})
        else:
            n = n + 1 if doubles else i + 1
            plan.append({"n": n, "source": i, "half": None})
    return plan


def double_ratio(source: PageSource, indices: list[int]) -> float:
    if not indices:
        return 0.0
    sample = indices if len(indices) <= 40 else indices[:: max(1, len(indices) // 40)]
    return sum(1 for i in sample if preprocess.is_double(*source.size(i))) / len(sample)


def page_path(dir_ocr: Path, n: int) -> Path:
    return Path(dir_ocr) / f"p{n:04d}.json"


def image_path(dir_img: Path, n: int) -> Path:
    return Path(dir_img) / f"p{n:04d}.jpg"


def _write_json(path: Path, data: dict) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def page_done(dir_ocr: Path, dir_img: Path, n: int, params: OCRParams, item: dict | None = None) -> dict | None:
    """Saved result of page n, if it was done with the same parameters (and, with `item`, the same image/half)."""
    c = page_path(dir_ocr, n)
    if not c.exists() or not image_path(dir_img, n).exists():
        return None
    try:
        d = json.loads(c.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if d.get("hash") != params.hash():
        return None
    if item is not None and (d.get("source") != item["source"] or d.get("half") != item["half"]):
        return None
    return d


def prepare_sample(source: PageSource, i: int, dpi: int, dest: Path, half: int | None = None) -> Path:
    """Renders and prepares a page for quick language/model tests. Returns the PNG written to `dest`."""
    raw = source.render(i, dpi)
    pre, _ = prepare_page(raw, half)
    file = Path(dest) / f"sample_{i}.png"
    pre.save(file, dpi=(dpi, dpi), compress_level=1)
    return file


def process_page(item: dict, source: PageSource, params: OCRParams, exe: str,
                 dir_ocr: Path, dir_img: Path) -> dict:
    done = page_done(dir_ocr, dir_img, item["n"], params, item)
    if done is not None:
        return done
    t0 = time.monotonic()
    raw = source.render(item["source"], params.dpi)
    pre, info = preprocess.prepare(raw, item["half"])
    del raw

    rotated = 0
    with tempfile.TemporaryDirectory(prefix="ocr_") as tmp:
        file = Path(tmp) / "p.png"
        pre.save(file, dpi=(params.dpi, params.dpi), compress_level=1)
        words = tesseract.run_tsv(exe, file, params.lang, params.psm, params.dpi)
        conf = tesseract.mean_confidence(words)
        if conf < REORIENT_THRESHOLD:  # page sideways or upside down?
            o = tesseract.orientation(exe, file)
            if o and o[0] in (90, 180, 270) and o[1] >= 1.5:
                pre2 = preprocess.rotate_clockwise(pre, o[0])
                pre2.save(file, dpi=(params.dpi, params.dpi), compress_level=1)
                words2 = tesseract.run_tsv(exe, file, params.lang, params.psm, params.dpi)
                if tesseract.mean_confidence(words2) > conf + 5:
                    pre, words, conf, rotated = pre2, words2, tesseract.mean_confidence(words2), o[0]

    preprocess.shrink_for_review(pre).save(image_path(dir_img, item["n"]), quality=82)
    data = {
        "n": item["n"], "source": item["source"], "half": item["half"], "hash": params.hash(),
        "lang": params.lang, "width": pre.width, "height": pre.height, "angle": info["angle"],
        "rotation": rotated, "ink": info["ink"], "conf": round(conf, 1), "words": words,
        "seconds": round(time.monotonic() - t0, 1),
    }
    _write_json(page_path(dir_ocr, item["n"]), data)
    return data


def run(source: PageSource, plan: list[dict], params: OCRParams, exe: str, dir_ocr: Path, dir_img: Path,
        processes: int = 0, progress=None) -> tuple[dict[int, dict], dict[int, str]]:
    """Runs OCR on the pages of the plan that don't have a result yet. Returns ({n: page}, {n: error})."""
    dir_ocr, dir_img = Path(dir_ocr), Path(dir_img)
    dir_ocr.mkdir(parents=True, exist_ok=True)
    dir_img.mkdir(parents=True, exist_ok=True)
    workers = processes if processes > 0 else max(1, min(8, (os.cpu_count() or 2) - 1 or 1))
    results: dict[int, dict] = {}
    errors: dict[int, str] = {}
    total = len(plan)
    done = 0
    ex = ThreadPoolExecutor(max_workers=workers)
    try:
        futures = {ex.submit(process_page, it, source, params, exe, dir_ocr, dir_img): it for it in plan}
        for fut in as_completed(futures):
            it = futures[fut]
            done += 1
            try:
                results[it["n"]] = fut.result()
            except OCRError as e:
                errors[it["n"]] = str(e)
            except Exception as e:  # noqa: BLE001
                errors[it["n"]] = f"{type(e).__name__}: {e}"
            if progress:
                progress(done, total, it["n"], results.get(it["n"]))
    except BaseException:
        ex.shutdown(wait=False, cancel_futures=True)
        raise
    ex.shutdown(wait=True)
    return results, errors


def load_pages(dir_ocr: Path, numbers: list[int]) -> dict[int, dict]:
    out = {}
    for n in numbers:
        c = page_path(dir_ocr, n)
        if c.exists():
            out[n] = json.loads(c.read_text(encoding="utf-8"))
    return out
