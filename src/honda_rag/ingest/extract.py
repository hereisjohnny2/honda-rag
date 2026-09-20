"""Etapa 4: dados estruturados (torques, ferramentas especiais, part numbers, DTC)."""
from __future__ import annotations

import re
import statistics

from honda_rag.ingest.normalize import normalize, normalize_engine
from honda_rag.ingest.structure import Block, load_ocr

# ------------------------------------------------------------------ torques
TORQUE_RE = re.compile(
    r"(\d+(?:\.\d+)?) N·m \((\d+(?:\.\d+)?) kg-m,? (\d+(?:\.\d+)?) lb-ft\)")
FASTENER_RE = re.compile(r"^(\d{1,2}) x (\d(?:\.\d{1,2})?) mm$")

NM_PER_KGM = 9.80665
NM_PER_LBFT = 1.35582


def check_torque(nm: float, kgm: float, lbft: float) -> str | None:
    """Os três valores do manual são a mesma grandeza: divergência = erro de OCR ou do manual."""
    if abs(nm / NM_PER_KGM - kgm) > max(0.06, 0.03 * kgm):
        return f"{nm} N·m = {nm / NM_PER_KGM:.2f} kg-m, mas está {kgm}"
    if abs(nm / NM_PER_LBFT - lbft) > max(1.0, 0.03 * lbft):
        return f"{nm} N·m = {nm / NM_PER_LBFT:.1f} lb-ft, mas está {lbft}"
    return None


ENGINE_HEADING = re.compile(r"^((?:D\w{4}(?:,\s*)?)+)\s+engine", re.I)


def engine_headings(lines: list[dict]) -> list[tuple[int, list[str]]]:
    """Títulos do tipo 'D15B7, D15B8 engine:' com a posição y (valem até o próximo)."""
    out = []
    for l in lines:
        m = ENGINE_HEADING.match(l["norm"])
        if m and not l["in_figure"]:
            out.append((l["y0"], [normalize_engine(t) for t in re.findall(r"D\w{4}", m.group(1))]))
    return sorted(out)


STEP_LINE = re.compile(r"^(\d{1,2})\.\s+\S")


def _col(l: dict, split_x: int | None) -> int:
    if l["region"] in ("col1", "col2"):
        return 0 if l["region"] == "col1" else 1
    return 0 if split_x is None or l["x0"] < split_x else 1


def step_context(lines: list[dict], l: dict, split_x: int | None) -> tuple[int, str] | None:
    """Passo numerado imediatamente acima do callout, na mesma coluna (figura logo abaixo do passo)."""
    col = _col(l, split_x)
    steps = [s for s in lines if not s["in_figure"] and s["region"] in ("col1", "col2", "body")
             and STEP_LINE.match(s["norm"]) and s["y0"] < l["y0"] and _col(s, split_x) == col]
    if not steps:
        return None
    st = max(steps, key=lambda s: s["y0"])
    if l["y0"] - st["y0"] > 1900:      # longe demais: não é a figura desse passo
        return None
    nxt = [s["y0"] for s in lines if not s["in_figure"] and s["y0"] > st["y0"]
           and STEP_LINE.match(s["norm"]) and _col(s, split_x) == col]
    limit = min(nxt + [l["y0"]])
    body = [x for x in lines if not x["in_figure"] and st["y0"] <= x["y0"] < limit
            and x["region"] in ("col1", "col2", "body") and _col(x, split_x) == col]
    body.sort(key=lambda x: x["y0"])
    text = " ".join(x["norm"] for x in body[:4])
    m = STEP_LINE.match(text)
    return int(m.group(1)), re.sub(r"^\d{1,2}\.\s+", "", text)[:170]


def nearest_caps(caps: list[dict], l: dict, maxdist: int = 520) -> str | None:
    best, bd = None, maxdist
    for c in caps:
        dx = max(c["x0"] - l["x1"], l["x0"] - c["x1"], 0)
        dy = max(c["y0"] - l["y1"], l["y0"] - c["y1"], 0)
        d = (dx * dx + dy * dy) ** 0.5
        if d < bd:
            best, bd = c["norm"], d
    return best


def torque_specs(pdf_page: int, page_label: str | None, proc_title: str) -> tuple[list[dict], list[dict]]:
    """Torques de uma página, ligados ao rótulo (CAPS) mais próximo acima do callout."""
    o = load_ocr(pdf_page)
    lines = [dict(l, norm=normalize(l["text"])) for l in o["lines"] if l["region"] != "footer"]
    caps = [l for l in lines if l["in_figure"] and re.fullmatch(r"[A-Z0-9/()\- ]{4,}", l["norm"])
            and not FASTENER_RE.match(l["norm"])]
    heads = engine_headings(lines)
    specs, issues = [], []
    for l in lines:
        m = TORQUE_RE.search(l["norm"])
        if not m:
            continue
        nm, kgm, lbft = (float(x) for x in m.groups())
        # fixador: linha logo acima (ex.: "10 x 1.25 mm") ou no início desta linha
        fastener = None
        for f in lines:
            if abs(f["x0"] - l["x0"]) < 140 and 0 < l["y0"] - f["y1"] < 70:
                fm = FASTENER_RE.match(f["norm"])
                if fm:
                    fastener = f"{fm.group(1)} x {fm.group(2)} mm"
                    break
        anchor_y = (l["y0"] - 120) if fastener else l["y0"]
        # rótulo em CAPS imediatamente acima do callout, na mesma coluna
        cand = [c for c in caps if 0 <= anchor_y + 120 - c["y1"] < 220 and abs(c["x0"] - l["x0"]) < 120]
        label = max(cand, key=lambda c: c["y1"])["norm"] if cand else None
        step = step_context(lines, l, o["layout"].get("split_x"))
        near = None if label else nearest_caps(caps, l)
        spec = {
            "component": (label.title() if label else proc_title), "parameter": "torque",
            "step": step, "near_label": near,
            "value_text": m.group(0), "value_min": nm, "value_max": nm, "unit": "N·m",
            "fastener": fastener, "page": pdf_page, "page_label": page_label,
            "conf": l["conf"], "in_figure": l["in_figure"],
            "engines": next((e for y, e in reversed(heads) if y < l["y0"]), []),
        }
        specs.append(spec)
        bad = check_torque(nm, kgm, lbft)
        if bad or l["conf"] < 75:
            issues.append({"reason": "unit_mismatch" if bad else "low_conf", "page": pdf_page,
                           "candidates": {"spec": f"{spec['component']} {fastener or ''}".strip(),
                                          "text": m.group(0), "detail": bad or f"confiança {l['conf']}"}})
    return specs, issues


# ------------------------------------------------------------------ ferramentas especiais
TOOL_RE = re.compile(r"^07[A-Z0-9]{10}$")


def _norm_tool(raw: str) -> str | None:
    t = re.sub(r"[^A-Za-z0-9]", "", raw.upper())
    if t.startswith("O7"):
        t = "07" + t[2:]
    if not TOOL_RE.match(t):
        return None
    return f"{t[:5]}-{t[5:]}"


def special_tools(pdf_page: int) -> list[dict]:
    """Tabela 'Tool Number | Description | Q'ty | Page Reference' (linhas por posição y)."""
    o = load_ocr(pdf_page)
    words = [w for w in o["words"] if w[6] in ("body",) or w[6] == "sparse"]
    hdr = {w[0]: w for w in o["words"] if w[0] in ("Tool", "Description", "Page", "Q'ty")}
    if "Description" not in hdr or "Page" not in hdr or "Tool" not in hdr:
        return []
    desc_x = (hdr["Tool"][2] + hdr["Description"][2]) / 2   # o cabeçalho é centralizado na coluna
    qty_x = hdr["Q'ty"][2] - 60 if "Q'ty" in hdr else hdr["Page"][2] - 140
    ref_x = hdr["Page"][2] - 40
    y0 = hdr["Description"][5] + 10
    rows: list[list[list]] = []
    for w in sorted((w for w in words if w[3] > y0), key=lambda w: (w[3] + w[5]) / 2):
        cy = (w[3] + w[5]) / 2
        if rows and abs(cy - statistics.mean((r[3] + r[5]) / 2 for r in rows[-1])) < 22:
            rows[-1].append(w)
        else:
            rows.append([w])
    out = []
    for r in rows:
        left = "".join(w[0] for w in sorted(r, key=lambda w: w[2]) if 450 < w[2] < desc_x)
        tool = _norm_tool(left.replace("or", "", 1) if left.startswith("or") else left)
        if not tool:
            continue
        desc = " ".join(w[0] for w in sorted(r, key=lambda w: w[2]) if desc_x <= w[2] < qty_x)
        ref = " ".join(w[0] for w in sorted(r, key=lambda w: w[2]) if w[2] >= ref_x)
        out.append({"tool_number": tool, "name": normalize(desc) or None, "page_ref": normalize(ref) or None,
                    "alternate": left.startswith("or")})
    for a, b in zip(out, out[1:]):  # "07HAH-PJ7010B  or 07HAH-PJ7010C" compartilham a descrição
        if not a["name"] and b["alternate"]:
            a["name"], a["page_ref"] = b["name"], b["page_ref"]
    return out


# ------------------------------------------------------------------ peças e DTC
PART_RE = re.compile(r"Part No\.?\s*(\d{5}\s?[-–—]\s?\d{4}|\d{5}-[A-Z0-9]{3}-[A-Z0-9]{2,4})")
LIQUID_RE = re.compile(r"(?i)(liquid gasket|sealant|thread lock|grease)")


def part_numbers(blocks: list[Block]) -> list[dict]:
    out, seen = [], set()
    for b in blocks:
        for m in PART_RE.finditer(b.text):
            pn = re.sub(r"\s?[-–—]\s?", "-", m.group(1))
            if pn in seen:
                continue
            seen.add(pn)
            d = LIQUID_RE.search(b.text)
            out.append({"part_number": pn, "description": d.group(1).lower() if d else None, "page": b.page})
    return out


DTC_RE = re.compile(r"(?i)indicates code (\d{1,2})\s*:\s*(.+)")


def dtc_codes(blocks: list[Block]) -> list[dict]:
    out = []
    for b in blocks:
        m = DTC_RE.search(b.text)
        if m:
            out.append({"code": m.group(1), "description": m.group(2).strip(" ."), "page": b.page})
    return out
