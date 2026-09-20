"""Etapa 2: layout + OCR (Tesseract TSV) por página -> data/pages/{n}/ocr.json.

Cada palavra guarda texto, confiança e bbox (px do render de 300 DPI). Palavras dentro de
figuras são marcadas com `in_figure` (viram callouts, não texto corrido).
"""
from __future__ import annotations

import csv
import io
import json
import re
import subprocess
from concurrent.futures import ProcessPoolExecutor

import numpy as np
from PIL import Image

from honda_rag import config
from honda_rag.ingest import layout, rasterize

LABEL_RE = r"\b(\d{1,2})\s*[-–]\s*(\d{1,3})\b"


def _tesseract(img: Image.Image, psm: int, dx: int = 0, dy: int = 0, extra: tuple[str, ...] = ()) -> list[dict]:
    buf = io.BytesIO()
    img.save(buf, "PNG")
    res = subprocess.run(
        [config.TESSERACT_CMD, "stdin", "stdout", "--psm", str(psm), "-l", "eng",
         "-c", "preserve_interword_spaces=1", *extra, "tsv"],
        input=buf.getvalue(), capture_output=True, check=True)
    rows = csv.DictReader(io.StringIO(res.stdout.decode("utf-8", "replace")),
                          delimiter="\t", quoting=csv.QUOTE_NONE)
    words = []
    for r in rows:
        if r["level"] != "5" or not (r["text"] or "").strip():
            continue
        conf = float(r["conf"])
        if conf < 0:
            continue
        words.append({
            "text": r["text"].strip(), "conf": conf,
            "x0": int(r["left"]) + dx, "y0": int(r["top"]) + dy,
            "x1": int(r["left"]) + int(r["width"]) + dx,
            "y1": int(r["top"]) + int(r["height"]) + dy,
            "blk": int(r["block_num"]), "par": int(r["par_num"]), "line": int(r["line_num"]),
        })
    return words


def _in_box(w: dict, box: list[int], pad: int = 40) -> bool:
    cx, cy = (w["x0"] + w["x1"]) / 2, (w["y0"] + w["y1"]) / 2
    return box[0] - pad <= cx <= box[2] + pad and box[1] - pad <= cy <= box[3] + pad


def _group_lines(words: list[dict], region: str) -> list[dict]:
    lines: dict[tuple, list[dict]] = {}
    for w in words:
        lines.setdefault((region, w["blk"], w["par"], w["line"]), []).append(w)
    out = []
    for (_, blk, par, ln), ws in lines.items():
        ws.sort(key=lambda w: w["x0"])
        out.append({
            "region": region, "blk": blk, "text": " ".join(w["text"] for w in ws),
            "conf": round(float(np.mean([w["conf"] for w in ws])), 1),
            "x0": min(w["x0"] for w in ws), "y0": min(w["y0"] for w in ws),
            "x1": max(w["x1"] for w in ws), "y1": max(w["y1"] for w in ws),
            "in_figure": all(w.get("in_figure") for w in ws),
        })
    # ordem de leitura dentro da região: Tesseract já numera blocos em ordem
    out.sort(key=lambda l: (l["blk"], l["y0"], l["x0"]))
    return out


def _overlaps(w: dict, others: list[dict]) -> bool:
    cx, cy = (w["x0"] + w["x1"]) / 2, (w["y0"] + w["y1"]) / 2
    return any(o["x0"] - 4 <= cx <= o["x1"] + 4 and o["y0"] - 4 <= cy <= o["y1"] + 4 for o in others)


def _sparse_extras(img: Image.Image, known: list[dict], figs: list[list[int]]) -> list[dict]:
    """Palavras que só o modo esparso (psm 11) acha: callouts soltos em desenhos."""
    extras = [w for w in _tesseract(img, 11)
              if w["conf"] >= 60 and any(_in_box(w, f, 400) for f in figs)
              and not _overlaps(w, known)]
    extras.sort(key=lambda w: ((w["y0"] + w["y1"]) / 2, w["x0"]))
    line_id, last = 0, None
    for w in extras:
        cy = (w["y0"] + w["y1"]) / 2
        if last is not None and (abs(cy - last["cy"]) > 0.6 * (w["y1"] - w["y0"])
                                 or w["x0"] - last["x1"] > 90):
            line_id += 1
        last = {"cy": cy, "x1": w["x1"]}
        w.update(blk=999, par=1, line=line_id, in_figure=True, region="sparse")
    return extras


def _complete_torques(img: Image.Image, lines: list[dict]) -> None:
    """Callout de torque `N·m (x kg-m,` quebra em duas linhas e a segunda (`y lb-ft)`) sai ruim
    porque a seta encosta nela. Relê só a faixa logo abaixo, sem filtro de confiança."""
    for l in lines:
        if not l["in_figure"] or not re.search(r"kg-?\s?m,?\s*$", l["text"]):
            continue
        box = (l["x0"] - 10, l["y1"] + 2, l["x0"] + 330, l["y1"] + 58)
        ws = _tesseract(img.crop(box), 7, box[0], box[1])
        txt = " ".join(w["text"] for w in ws)
        m = re.search(r"(\d+)\s*[IilLW1|]b-?\s?ft\)?", txt)
        if m:
            l["text"] += f" {m.group(1)} lb-ft)"
            l["y1"] = max(l["y1"], max(w["y1"] for w in ws))
            l["completed"] = True


def _is_junk(l: dict) -> bool:
    alnum = re.findall(r"[A-Za-z0-9]", l["text"])
    return len(alnum) < 2 or (l["conf"] < 70 and not re.search(r"\d|[A-Z]{3}", l["text"]))


def ocr_page(pdf_page: int, force: bool = False) -> dict:
    d = rasterize.page_dir(pdf_page)
    out_path = d / "ocr.json"
    if out_path.exists() and not force:
        return json.loads(out_path.read_text(encoding="utf-8"))
    png = rasterize.rasterize_page(pdf_page)
    img = Image.open(png).convert("L")
    w, h = img.size
    ink = layout.load_binary(png)
    lay = layout.detect(ink)
    figs = [] if lay["kind"] == "table" else layout.figure_boxes(ink)

    regions: list[tuple[str, tuple[int, int, int, int], int]] = []
    if lay["kind"] == "columns":
        sx = lay["split_x"]
        top = int(h * 0.04)
        bot = int(h * 0.90)
        regions = [("header", (0, 0, w, top + int(h * 0.09)), 6),
                   ("col1", (0, int(h * 0.13), sx - 6, bot), 3),
                   ("col2", (sx + 6, int(h * 0.13), w, bot), 3)]
    elif lay["kind"] == "table":
        regions = [("body", (0, 0, w, int(h * 0.90)), 6)]
    else:
        regions = [("body", (0, 0, w, int(h * 0.90)), 3)]
    regions.append(("footer", (0, int(h * 0.90), w, h), 6))

    lines: list[dict] = []
    all_words: list[dict] = []
    for name, (x0, y0, x1, y1), psm in regions:
        ws = _tesseract(img.crop((x0, y0, x1, y1)), psm, x0, y0)
        for wd in ws:
            wd["in_figure"] = any(_in_box(wd, f) for f in figs)
            wd["region"] = name
        all_words.extend(ws)
        lines.extend(_group_lines(ws, name))

    if figs:
        extras = _sparse_extras(img, all_words, figs)
        all_words.extend(extras)
        lines.extend(l for l in _group_lines(extras, "sparse") if not _is_junk(l))
    _complete_torques(img, lines)

    result = {
        "pdf_page": pdf_page, "engine": "tesseract", "size": [w, h],
        "layout": {**lay, "figures": figs},
        "lines": lines,
        "words": [[w_["text"], round(w_["conf"]), w_["x0"], w_["y0"], w_["x1"], w_["y1"],
                   w_["region"], int(bool(w_.get("in_figure")))] for w_ in all_words],
        "mean_conf": round(float(np.mean([w_["conf"] for w_ in all_words])), 1) if all_words else 0,
    }
    out_path.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    return result


def _job(args):
    return ocr_page(*args)["mean_conf"]


def run(pages: list[int], force: bool = False, workers: int = 4) -> None:
    rasterize.run(pages, force, workers)
    with ProcessPoolExecutor(max_workers=workers) as ex:
        for i, c in enumerate(ex.map(_job, [(p, force) for p in pages]), 1):
            if i % 10 == 0 or i == len(pages):
                print(f"  ocr {i}/{len(pages)} (conf da última: {c})", flush=True)
