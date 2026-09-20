"""Segundo OCR só do rodapé, para recuperar o rótulo impresso da página (`6-3`).

O OCR da página inteira confunde os dígitos do rodapé (9 -> J, 6 -> 0/O). Aqui o canto inferior é
relido a 600 DPI, com lista de caracteres permitidos (só dígitos e hífen), o que elimina essas trocas.
Resultado em data/pages/{n}/footer.json; usado por `structure.infer_labels` como âncora extra.
"""
from __future__ import annotations

import json
import re
from concurrent.futures import ProcessPoolExecutor

import numpy as np
import pymupdf
from PIL import Image

from honda_rag import config
from honda_rag.ingest import ocr, rasterize

LABEL = re.compile(r"^(\d{1,2})-(\d{1,3})$")
WHITELIST = ("-c", "tessedit_char_whitelist=0123456789-")


def _corner_text(img: Image.Image, x0: int, x1: int, y0: int, y1: int) -> tuple[str, float]:
    band = img.crop((x0, y0, x1, y1))
    ink = np.asarray(band) < 140
    rows = np.where(ink.sum(axis=1) > 2)[0]
    if len(rows) == 0:
        return "", 0.0
    # último bloco de tinta com tamanho de rótulo (ignora manchas do scan e riscos soltos)
    groups = [g for g in np.split(rows, np.where(np.diff(rows) > 40)[0] + 1) if len(g) >= 40]
    if not groups:
        return "", 0.0
    top, bottom = int(groups[-1][0]), int(groups[-1][-1])
    cols = np.where(ink[top:bottom + 1].sum(axis=0) > 1)[0]
    if len(cols) == 0:
        return "", 0.0
    box = (max(int(cols[0]) - 20, 0), max(top - 15, 0), min(int(cols[-1]) + 20, band.width),
           min(bottom + 15, band.height))
    tight = band.crop(box)
    if tight.height > 260 or tight.width > 900:      # não parece um rótulo
        return "", 0.0
    tight = tight.resize((tight.width // 2, tight.height // 2))   # 600 -> 300 DPI: dígitos ~45 px
    tight = Image.fromarray(np.pad(np.asarray(tight), 30, constant_values=255))
    ws = ocr._tesseract(tight, 7, extra=WHITELIST)
    if not ws:
        return "", 0.0
    return "".join(w["text"] for w in ws), float(np.mean([w["conf"] for w in ws]))


def read_footer(pdf_page: int, force: bool = False) -> dict:
    path = rasterize.page_dir(pdf_page) / "footer.json"
    if path.exists() and not force:
        return json.loads(path.read_text(encoding="utf-8"))
    with pymupdf.open(config.PDF_PATH) as doc:
        pix = doc[pdf_page - 1].get_pixmap(dpi=600, colorspace=pymupdf.csGRAY)
    img = Image.frombytes("L", (pix.width, pix.height), pix.samples)
    W, H = img.size
    y0, y1 = int(H * 0.895), int(H * 0.985)
    res = {}
    for side, (x0, x1) in {"left": (0, int(W * 0.32)), "right": (int(W * 0.68), W)}.items():
        text, conf = _corner_text(img, x0, x1, y0, y1)
        res[side] = {"text": text, "conf": round(conf, 1)}
    good = [(v["conf"], v["text"]) for v in res.values() if LABEL.match(v["text"])]
    res["label"] = max(good)[1] if good else None
    rasterize.page_dir(pdf_page).mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(res), encoding="utf-8")
    return res


def _job(args):
    return read_footer(*args)["label"]


def run(pages: list[int], force: bool = False, workers: int = 4) -> None:
    with ProcessPoolExecutor(max_workers=workers) as ex:
        for i, _ in enumerate(ex.map(_job, [(p, force) for p in pages]), 1):
            if i % 100 == 0 or i == len(pages):
                print(f"  footer {i}/{len(pages)}", flush=True)
