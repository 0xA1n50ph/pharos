"""Preparo da imagem antes do OCR: tons de cinza, iluminação uniforme, contraste, páginas duplas e inclinação.

Só Pillow + numpy. As coordenadas que o Tesseract devolve valem para a imagem preparada, que também é a
que aparece na tela de revisão.
"""
from __future__ import annotations

import numpy as np
from PIL import Image, ImageFilter, ImageOps

LIMIAR_DUPLA = 1.2  # largura/altura acima disso = duas páginas lado a lado


def e_dupla(largura: float, altura: float) -> bool:
    return altura > 0 and largura / altura > LIMIAR_DUPLA


def cinza(img: Image.Image) -> Image.Image:
    return img if img.mode == "L" else img.convert("L")


def uniformizar_iluminacao(g: Image.Image) -> Image.Image:
    """Divide pelo fundo estimado (sombras, lombada, foto com luz irregular) e estica o contraste."""
    w, h = g.size
    f = max(1, min(w, h) // 200)
    pequena = g.resize((max(1, w // f), max(1, h // f)), Image.BOX)
    k = max(3, (int(0.07 * min(pequena.size)) * 2) | 1)
    fundo = pequena.filter(ImageFilter.MaxFilter(k)).filter(ImageFilter.GaussianBlur(radius=k / 3))
    fundo = fundo.resize((w, h), Image.BILINEAR)
    a = np.asarray(g, dtype=np.float32)
    b = np.maximum(np.asarray(fundo, dtype=np.float32), 1.0)
    saida = np.clip(a / b * 255.0, 0, 255).astype(np.uint8)
    return ImageOps.autocontrast(Image.fromarray(saida, "L"), cutoff=0.3)


def _tinta(g_pequena: Image.Image) -> np.ndarray:
    return (np.asarray(g_pequena) < 160)


def fracao_de_tinta(g: Image.Image) -> float:
    w, h = g.size
    f = max(1, w // 600)
    return float(_tinta(g.resize((max(1, w // f), max(1, h // f)), Image.BOX)).mean())


def _calha(g: Image.Image) -> tuple[int, int]:
    """Faixa da lombada, em pixels: a metade esquerda vai até x0 e a direita começa em x1.

    Com sombra escura na lombada, a faixa é a própria sombra (some das duas metades). Sem sombra, corta no meio
    da faixa vazia mais larga perto do centro."""
    w, h = g.size
    f = max(1, w // 800)
    pequena = _tinta(g.resize((max(1, w // f), max(1, h // f)), Image.BOX))
    perfil = pequena.mean(axis=0)
    n = len(perfil)
    janela = max(3, n // 50)
    suave = np.convolve(perfil, np.ones(janela) / janela, mode="same")
    a, b = int(0.30 * n), int(0.70 * n)
    if b - a < 3:
        return w // 2, w // 2
    miolo = suave[a:b]
    pico = int(np.argmax(miolo))
    if miolo[pico] >= 0.5:  # sombra escura em quase toda a altura
        meia = miolo[pico] * 0.5
        i0, i1 = pico, pico
        while i0 > 0 and miolo[i0 - 1] >= meia:
            i0 -= 1
        while i1 < len(miolo) - 1 and miolo[i1 + 1] >= meia:
            i1 += 1
        if (i1 - i0 + 1) <= 0.15 * n:
            return max(1, (a + i0) * f), min(w - 1, (a + i1 + 1) * f)
    limiar = miolo.min() + 0.15 * (miolo.max() - miolo.min())
    livre = miolo <= limiar
    melhor, ini = (0, 0), None
    for k, v in enumerate(list(livre) + [False]):
        if v and ini is None:
            ini = k
        elif not v and ini is not None:
            if k - ini > melhor[1] - melhor[0]:
                melhor = (ini, k)
            ini = None
    centro = a + (melhor[0] + melhor[1]) // 2 if melhor[1] > melhor[0] else a + int(np.argmin(miolo))
    x = min(w - 1, max(1, centro * f))
    return x, x


def dividir_dupla(g: Image.Image, metade: int) -> Image.Image:
    """Separa as duas páginas de uma imagem larga. metade 0 = esquerda, 1 = direita; a lombada fica de fora."""
    w, h = g.size
    x0, x1 = _calha(g)
    return g.crop((0, 0, x0, h)) if metade == 0 else g.crop((x1, 0, w, h))


def estimar_inclinacao(g: Image.Image, maximo: float = 5.0) -> float:
    """Graus (anti-horário positivo, como Image.rotate) que endireitam as linhas de texto."""
    w, h = g.size
    f = max(1, w // 700)
    tinta = Image.fromarray((_tinta(g.resize((max(1, w // f), max(1, h // f)), Image.BOX)) * 255).astype(np.uint8), "L")
    if np.asarray(tinta).mean() < 0.15:  # página praticamente em branco (menos de ~0,06% de tinta)
        return 0.0

    def nitidez(ang: float) -> float:
        r = tinta.rotate(ang, resample=Image.BILINEAR, fillcolor=0)
        perfil = np.asarray(r, dtype=np.float32).sum(axis=1)
        return float(np.sum(np.diff(perfil) ** 2))

    base = nitidez(0.0)
    if base <= 0:
        return 0.0
    grosso = max(np.arange(-maximo, maximo + 0.01, 0.5), key=nitidez)
    fino = max(np.arange(grosso - 0.5, grosso + 0.51, 0.1), key=nitidez)
    if abs(fino) < 0.15 or nitidez(fino) < 1.02 * base:
        return 0.0
    return float(round(fino, 2))


def preparar(img: Image.Image, metade: int | None = None) -> tuple[Image.Image, dict]:
    """Devolve (imagem preparada em tons de cinza, {'angulo', 'tinta'})."""
    g = uniformizar_iluminacao(cinza(img))
    if metade is not None:
        g = dividir_dupla(g, metade)
    ang = estimar_inclinacao(g)
    if ang:
        g = g.rotate(ang, resample=Image.BICUBIC, fillcolor=255)
    return g, {"angulo": ang, "tinta": round(fracao_de_tinta(g), 4)}


def girar_horario(img: Image.Image, graus: int) -> Image.Image:
    return img.rotate(-graus, expand=True, fillcolor=255)


def reduzir_para_revisao(img: Image.Image, largura_max: int = 1500) -> Image.Image:
    if img.width <= largura_max:
        return img
    return img.resize((largura_max, round(img.height * largura_max / img.width)), Image.LANCZOS)
