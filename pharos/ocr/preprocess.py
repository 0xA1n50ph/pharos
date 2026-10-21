"""Image preparation before OCR: grayscale, even lighting, contrast, double pages and skew.

Only Pillow + numpy. The coordinates Tesseract returns apply to the prepared image, which is also the
one shown in the review screen.
"""
from __future__ import annotations

import numpy as np
from PIL import Image, ImageFilter, ImageOps

DOUBLE_THRESHOLD = 1.2  # width/height above this = two pages side by side


def is_double(width: float, height: float) -> bool:
    return height > 0 and width / height > DOUBLE_THRESHOLD


def grayscale(img: Image.Image) -> Image.Image:
    return img if img.mode == "L" else img.convert("L")


def even_lighting(g: Image.Image) -> Image.Image:
    """Divides by the estimated background (shadows, spine, photo with uneven light) and stretches the contrast."""
    w, h = g.size
    f = max(1, min(w, h) // 200)
    small = g.resize((max(1, w // f), max(1, h // f)), Image.BOX)
    k = max(3, (int(0.07 * min(small.size)) * 2) | 1)
    bg = small.filter(ImageFilter.MaxFilter(k)).filter(ImageFilter.GaussianBlur(radius=k / 3))
    bg = bg.resize((w, h), Image.BILINEAR)
    a = np.asarray(g, dtype=np.float32)
    b = np.maximum(np.asarray(bg, dtype=np.float32), 1.0)
    out = np.clip(a / b * 255.0, 0, 255).astype(np.uint8)
    return ImageOps.autocontrast(Image.fromarray(out, "L"), cutoff=0.3)


def _ink(g_small: Image.Image) -> np.ndarray:
    return (np.asarray(g_small) < 160)


def ink_fraction(g: Image.Image) -> float:
    w, h = g.size
    f = max(1, w // 600)
    return float(_ink(g.resize((max(1, w // f), max(1, h // f)), Image.BOX)).mean())


def _gutter(g: Image.Image) -> tuple[int, int]:
    """Spine band, in pixels: the left half runs to x0 and the right one starts at x1.

    With a dark shadow on the spine, the band is the shadow itself (removed from both halves). Without a shadow,
    it cuts in the middle of the widest empty band near the centre."""
    w, h = g.size
    f = max(1, w // 800)
    small = _ink(g.resize((max(1, w // f), max(1, h // f)), Image.BOX))
    profile = small.mean(axis=0)
    n = len(profile)
    window = max(3, n // 50)
    smooth = np.convolve(profile, np.ones(window) / window, mode="same")
    a, b = int(0.30 * n), int(0.70 * n)
    if b - a < 3:
        return w // 2, w // 2
    middle = smooth[a:b]
    peak = int(np.argmax(middle))
    if middle[peak] >= 0.5:  # dark shadow over almost the whole height
        half = middle[peak] * 0.5
        i0, i1 = peak, peak
        while i0 > 0 and middle[i0 - 1] >= half:
            i0 -= 1
        while i1 < len(middle) - 1 and middle[i1 + 1] >= half:
            i1 += 1
        if (i1 - i0 + 1) <= 0.15 * n:
            return max(1, (a + i0) * f), min(w - 1, (a + i1 + 1) * f)
    threshold = middle.min() + 0.15 * (middle.max() - middle.min())
    free = middle <= threshold
    best, start = (0, 0), None
    for k, v in enumerate(list(free) + [False]):
        if v and start is None:
            start = k
        elif not v and start is not None:
            if k - start > best[1] - best[0]:
                best = (start, k)
            start = None
    center = a + (best[0] + best[1]) // 2 if best[1] > best[0] else a + int(np.argmin(middle))
    x = min(w - 1, max(1, center * f))
    return x, x


def split_double(g: Image.Image, half: int) -> Image.Image:
    """Separates the two pages of a wide image. half 0 = left, 1 = right; the spine is left out."""
    w, h = g.size
    x0, x1 = _gutter(g)
    return g.crop((0, 0, x0, h)) if half == 0 else g.crop((x1, 0, w, h))


def estimate_skew(g: Image.Image, maximum: float = 5.0) -> float:
    """Degrees (counter-clockwise positive, like Image.rotate) that straighten the text lines."""
    w, h = g.size
    f = max(1, w // 700)
    ink = Image.fromarray((_ink(g.resize((max(1, w // f), max(1, h // f)), Image.BOX)) * 255).astype(np.uint8), "L")
    if np.asarray(ink).mean() < 0.15:  # practically blank page (less than ~0.06% ink)
        return 0.0

    def sharpness(ang: float) -> float:
        r = ink.rotate(ang, resample=Image.BILINEAR, fillcolor=0)
        profile = np.asarray(r, dtype=np.float32).sum(axis=1)
        return float(np.sum(np.diff(profile) ** 2))

    base = sharpness(0.0)
    if base <= 0:
        return 0.0
    coarse = max(np.arange(-maximum, maximum + 0.01, 0.5), key=sharpness)
    fine = max(np.arange(coarse - 0.5, coarse + 0.51, 0.1), key=sharpness)
    if abs(fine) < 0.15 or sharpness(fine) < 1.02 * base:
        return 0.0
    return float(round(fine, 2))


def prepare(img: Image.Image, half: int | None = None) -> tuple[Image.Image, dict]:
    """Returns (prepared grayscale image, {'angle', 'ink'})."""
    g = even_lighting(grayscale(img))
    if half is not None:
        g = split_double(g, half)
    ang = estimate_skew(g)
    if ang:
        g = g.rotate(ang, resample=Image.BICUBIC, fillcolor=255)
    return g, {"angle": ang, "ink": round(ink_fraction(g), 4)}


def rotate_clockwise(img: Image.Image, degrees: int) -> Image.Image:
    return img.rotate(-degrees, expand=True, fillcolor=255)


def shrink_for_review(img: Image.Image, max_width: int = 1500) -> Image.Image:
    if img.width <= max_width:
        return img
    return img.resize((max_width, round(img.height * max_width / img.width)), Image.LANCZOS)
