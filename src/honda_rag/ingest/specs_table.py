"""Parser das tabelas "Standards and Service Limits" (seção 3 do manual).

Colunas (pelas réguas verticais): Componente | Medição | Standard (New) | Service Limit.
Linhas de componente = intervalos entre réguas horizontais. Células mescladas (rótulo, motor)
são preenchidas para baixo (fill-down) dentro do componente.
"""
from __future__ import annotations

import re
import statistics

from honda_rag.ingest.normalize import normalize, normalize_engine

ENGINE_TOK = re.compile(r"^D[1Il][5S6\d][A-Z\d]\d,?$")
SIDES = {"IN", "EX"}
SUBS = {"Primary", "Secondary", "Mid", "Nominal", "Minimum", "Maximum", "variation"}
DASH_ONLY = re.compile(r"^[-—–_~=.|]{2,}$|^[-—–_]$")
NUM = r"[\d,]*\.?\d+"
RANGE_RE = re.compile(rf"^\s*({NUM})(?:\s*-\s*({NUM}))?\s*(?:\(\s*({NUM})(?:\s*-\s*({NUM}))?\s*\))?")


def _num(s: str | None) -> float | None:
    return float(s.replace(",", "")) if s else None


def _rows(words: list[list], y_lo: int, y_hi: int) -> list[list[list]]:
    ws = sorted((w for w in words if y_lo <= (w[3] + w[5]) / 2 < y_hi), key=lambda w: (w[3] + w[5]) / 2)
    if not ws:
        return []
    tol = 0.6 * statistics.median(w[5] - w[3] for w in ws)
    rows, cur, cy = [], [ws[0]], (ws[0][3] + ws[0][5]) / 2
    for w in ws[1:]:
        c = (w[3] + w[5]) / 2
        if abs(c - cy) <= tol:
            cur.append(w)
        else:
            rows.append(sorted(cur, key=lambda w: w[2]))
            cur, cy = [w], c
    rows.append(sorted(cur, key=lambda w: w[2]))
    return rows


_BORDER = "|[]{}_"


def _clean(w: list) -> list | None:
    t = w[0].strip(_BORDER + " ")
    if not t or re.fullmatch(r"[|\[\]{}_.,'`\/]+", t):
        return None
    return [t, *w[1:]]


def _cell_text(ws: list[list]) -> str:
    return normalize(" ".join(w[0] for w in ws))


def _is_none(text: str) -> bool:
    t = text.strip()
    return not t or bool(DASH_ONLY.match(t.replace(" ", "")))


def check_units(value_text: str) -> str | None:
    """Confere mm × pol. Retorna a descrição da incoerência ou None."""
    m = RANGE_RE.match(value_text)
    if not m or not m.group(3):
        return None
    mm_lo, mm_hi, in_lo, in_hi = (_num(g) for g in m.groups())
    pairs = [(mm_lo, in_lo)] + ([(mm_hi, in_hi)] if mm_hi and in_hi else [])
    for mm, inch in pairs:
        if mm is None or inch is None or mm == 0:
            continue
        if abs(mm / 25.4 - inch) > max(0.0006, 0.012 * inch):
            return f"{mm} mm = {mm / 25.4:.4f} in, mas o manual imprime {inch}"
    return None


def parse_page(ocr: dict, pdf_page: int) -> tuple[list[dict], list[dict]]:
    """-> (specs, issues). Cada spec: component, parameter, value_text, ..."""
    lay = ocr["layout"]
    vr, hr = lay["vertical_rules"], lay["horizontal_rules"]
    if len(vr) not in (4, 5) or len(hr) < 6:
        return [], []
    # réguas: moldura | componente | medição | standard | limite | moldura
    cols = list(vr)
    if len(vr) == 4:  # a divisória Standard/Limit não foi detectada: interpola (0.345 da largura)
        cols = [vr[0], vr[1], vr[2], round(vr[3] - 0.345 * (vr[3] - vr[2])), vr[3]]
    bounds = list(zip(cols[:-1], cols[1:]))  # (comp)(meas)(std)(limit)
    words = [c for c in (_clean(w) for w in ocr["words"] if w[6] == "body") if c]

    section = ""
    for w in ocr["lines"]:
        m = re.search(r"Section\s*(\d+)", w["text"])
        if m and w["y0"] < hr[0]:
            section = normalize(re.sub(r"[^\w/ \-]+", " ", w["text"]).strip())
            break

    specs, issues = [], []
    for g0, g1 in zip(hr[:-1], hr[1:]):
        if g1 - g0 < 40:
            continue
        rows = _rows(words, g0 + 4, g1 - 2)
        comp_words = [w for r in rows for w in r if bounds[0][0] < w[2] < bounds[0][1]]
        component = normalize(" ".join(w[0] for w in sorted(comp_words, key=lambda w: (w[3], w[2]))))
        component = re.sub(r"(\w)- (\w)", r"\1\2", component)
        label, pending, cur_engines, prev_side = "", "", [], None
        for r in rows:
            cells = [[w for w in r if b0 <= (w[2] + w[4]) / 2 < b1] for b0, b1 in bounds]
            meas, std, lim = cells[1], _cell_text(cells[2]), _cell_text(cells[3])
            label_w, sides, subs, engs = [], [], [], []
            for w in meas:
                t = w[0]
                parts = [x for x in t.split(",") if x]
                if parts and all(ENGINE_TOK.match(x) for x in parts):
                    engs.extend(normalize_engine(x) for x in parts)
                elif t in SIDES:
                    sides.append(t)
                elif t in SUBS:
                    subs.append(t)
                else:
                    label_w.append(t)
            if engs:
                cur_engines = engs
            has_val = bool(std.strip() or lim.strip())
            ltxt = normalize(" ".join(label_w)).replace("1.D.", "I.D.")
            if ltxt:
                if has_val:
                    label, pending = (pending + " " + ltxt).strip(), ""
                    if not engs:
                        cur_engines = []
                else:
                    pending = (pending + " " + ltxt).strip()
                    if not engs:
                        cur_engines = []
            if not has_val or "MEASUREMENT" in ltxt:
                continue
            if ltxt or engs:
                prev_side = None
            side = sides[0] if sides else prev_side
            prev_side = side
            cond = " ".join(subs)
            base = {
                "component": f"{component}: {label}".strip(": ") if label else component,
                "side": side, "conditions": " ".join(x for x in (cond, section) if x) or None,
                "engines": list(cur_engines), "page": pdf_page,
            }
            for param, text in (("standard", std), ("service_limit", lim)):
                if _is_none(text) or not re.search(r"\d", text):
                    continue
                spec = {**base, "parameter": param, "value_text": text}
                m = RANGE_RE.match(text)
                if m:
                    spec["value_min"], spec["value_max"] = _num(m.group(1)), _num(m.group(2) or m.group(1))
                    spec["unit"] = "kPa" if "kPa" in (label + pending) else "mm"
                specs.append(spec)
                bad = check_units(text)
                if bad:
                    issues.append({"reason": "unit_mismatch", "page": pdf_page,
                                   "candidates": {"spec": f'{spec["component"]} {cond}'.strip(),
                                                  "text": text, "detail": bad,
                                                  "note": "possível erro do próprio manual"}})
    return specs, issues
