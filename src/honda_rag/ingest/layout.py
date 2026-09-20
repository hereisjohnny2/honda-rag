"""Layout leve por análise de traços: régua vertical de duas colunas, tabelas e figuras.

Sem modelo de layout: o manual é limpo (1-bit, 600 DPI), então réguas e blocos de tinta bastam
para o piloto. Coordenadas em px do render de 300 DPI (page.png).
"""
from __future__ import annotations

import numpy as np
from PIL import Image
from scipy import ndimage


def load_binary(png_path) -> np.ndarray:
    """True = tinta."""
    return np.asarray(Image.open(png_path).convert("L")) < 140


def _long_runs(mask: np.ndarray, min_len: int, axis: int) -> list[tuple[int, int, int]]:
    """Segmentos retos (posição, início, fim) com pelo menos min_len px de tinta contínua.

    axis=0 -> linhas verticais (posição = x); axis=1 -> horizontais (posição = y).
    """
    m = mask if axis == 0 else mask.T
    h, w = m.shape
    out = []
    for x in range(w):
        col = m[:, x]
        if col.sum() < min_len:
            continue
        d = np.diff(np.concatenate([[0], col.astype(np.int8), [0]]))
        starts, ends = np.where(d == 1)[0], np.where(d == -1)[0]
        for s, e in zip(starts, ends):
            if e - s >= min_len:
                out.append((x, int(s), int(e)))
    return out


def _cluster(pos: list[int], gap: int = 8) -> list[int]:
    if not pos:
        return []
    pos = sorted(pos)
    groups, cur = [], [pos[0]]
    for p in pos[1:]:
        if p - cur[-1] <= gap:
            cur.append(p)
        else:
            groups.append(cur)
            cur = [p]
    groups.append(cur)
    return [int(np.mean(g)) for g in groups]


def detect(ink: np.ndarray) -> dict:
    """Retorna {'kind': 'columns'|'table'|'single', 'split_x': int|None, 'rules': {...}}."""
    h, w = ink.shape
    body = ink[int(h * 0.10):int(h * 0.93)]
    vert = _long_runs(body, int(h * 0.20), axis=0)
    vx = _cluster([x for x, _, _ in vert])
    kind, split = "single", None
    mid = [x for x in vx if 0.42 * w < x < 0.58 * w]
    if len(vx) >= 3:
        kind = "table"
    elif len(mid) == 1:  # a outra régua possível é a moldura da esquerda
        kind, split = "columns", mid[0]
    horiz = _long_runs(ink, int(w * 0.45), axis=1)
    hy = _cluster([y for y, _, _ in horiz])
    return {"kind": kind, "split_x": split, "vertical_rules": vx, "horizontal_rules": hy}


def figure_boxes(ink: np.ndarray, min_side: int = 260) -> list[list[int]]:
    """Regiões de desenho: blocos grandes de tinta densa e irregular (não texto).

    Texto vira componentes pequenos e alinhados; o desenho de linha vira um componente grande
    depois de dilatar. Retorna [x0, y0, x1, y1] em px de 300 DPI.
    """
    small = ink[::4, ::4]  # 75 DPI
    # tira réguas e molduras (senão tudo vira um componente só)
    hl = ndimage.binary_opening(small, structure=np.ones((1, 60), bool))
    vl = ndimage.binary_opening(small, structure=np.ones((60, 1), bool))
    small = small & ~ndimage.binary_dilation(hl | vl, iterations=2)
    dil = ndimage.binary_dilation(small, iterations=4)
    lab, n = ndimage.label(dil)
    boxes = []
    for sl in ndimage.find_objects(lab):
        y0, y1 = sl[0].start * 4, sl[0].stop * 4
        x0, x1 = sl[1].start * 4, sl[1].stop * 4
        bw, bh = x1 - x0, y1 - y0
        if bw < min_side or bh < min_side:
            continue
        region = ink[y0:y1, x0:x1]
        density = region.mean()
        # linhas de tabela/régua: caixas enormes e vazias por dentro
        if density < 0.012:
            continue
        boxes.append([x0, y0, x1, y1])
    return boxes
