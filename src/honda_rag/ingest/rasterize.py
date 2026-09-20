"""Etapa 1: PDF -> PNG 300 DPI (cinza, para OCR) + WebP 150 DPI (para a UI)."""
from __future__ import annotations

from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import pymupdf
from PIL import Image

from honda_rag import config

OCR_DPI = 300
VIEW_DPI = 150


def page_dir(pdf_page: int) -> Path:
    return config.PAGES_DIR / str(pdf_page)


def rasterize_page(pdf_page: int, force: bool = False) -> Path:
    out = page_dir(pdf_page)
    png, view = out / "page.png", out / "view.webp"
    if png.exists() and view.exists() and not force:
        return png
    out.mkdir(parents=True, exist_ok=True)
    with pymupdf.open(config.PDF_PATH) as doc:
        page = doc[pdf_page - 1]
        pix = page.get_pixmap(dpi=OCR_DPI, colorspace=pymupdf.csGRAY)
        pix.save(png)
        small = page.get_pixmap(dpi=VIEW_DPI, colorspace=pymupdf.csGRAY)
        Image.frombytes("L", (small.width, small.height), small.samples).save(view, quality=80)
    return png


def _job(args: tuple[int, bool]) -> int:
    rasterize_page(*args)
    return args[0]


def run(pages: list[int], force: bool = False, workers: int = 4) -> None:
    with ProcessPoolExecutor(max_workers=workers) as ex:
        for i, p in enumerate(ex.map(_job, [(p, force) for p in pages]), 1):
            if i % 10 == 0 or i == len(pages):
                print(f"  rasterize {i}/{len(pages)} (p. {p})", flush=True)
