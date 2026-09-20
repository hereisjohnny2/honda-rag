"""Etapa 3: estrutura. Rótulos de página, seções, procedimentos (via sumário) e texto limpo."""
from __future__ import annotations

import difflib
import json
import re
from dataclasses import dataclass, field

from honda_rag import config
from honda_rag.ingest import rasterize
from honda_rag.ingest.normalize import normalize

# Seções do manual (número impresso -> título como aparece no sumário). A 0 é a "Special Information"
# (sem numeração). As páginas de abertura vêm do próprio sumário (`_derive_starts`).
SECTION_TITLES = {
    0: "Special Information", 1: "General Information", 3: "Specifications", 4: "Maintenance",
    5: "Engine", 6: "Cylinder Head/Valve Train", 7: "Engine Block", 8: "Engine Lubrication",
    9: "Intake Manifold/Exhaust System", 10: "Cooling", 11: "Fuel and Emissions", 12: "Clutch",
    13: "Manual Transmission", 14: "Automatic Transmission", 15: "Differential", 16: "Driveshafts",
    17: "Steering", 18: "Suspension", 19: "Brakes", 20: "Body", 21: "Heater", 22: "Air Conditioner",
    23: "Electrical",
}


def _collapse(title: str) -> str:
    """O sumário do ManualsLib repete alguns títulos ("Heater Heater Heater"): reduz à unidade."""
    w = title.split()
    for k in range(1, len(w) // 2 + 1):
        if len(w) % k == 0 and w[:k] * (len(w) // k) == w:
            return " ".join(w[:k])
    return title


def _derive_starts() -> dict[int, int]:
    """página do PDF -> número da seção, pela primeira entrada do sumário com o título da seção."""
    toc = json.loads(config.TOC_PATH.read_text(encoding="utf-8"))
    first: dict[str, int] = {}
    for e in sorted(toc, key=lambda e: (e["pdf_page"], e["order"])):
        first.setdefault(_collapse(e["title"]), e["pdf_page"])
    return {first[t]: n for n, t in SECTION_TITLES.items() if t in first}


SECTION_STARTS = _derive_starts()

LABEL_STRICT = re.compile(r"^(\d{1,2})-(\d{1,3})$")

OPS = [("illustrated index", "illustrated_index"), ("troubleshooting", "troubleshooting"),
       ("removal", "removal"), ("installation", "installation"), ("inspection", "inspection"),
       ("adjustment", "adjustment"), ("replacement", "replacement"), ("replace", "replacement"),
       ("overhaul", "overhaul"), ("specifications", "specs"), ("standards and service", "specs"),
       ("design and operation", "description"), ("description", "description")]


def section_of(pdf_page: int) -> int | None:
    sec = None
    for start, n in sorted(SECTION_STARTS.items()):
        if pdf_page >= start:
            sec = n
    return sec


def load_ocr(pdf_page: int) -> dict:
    return json.loads((rasterize.page_dir(pdf_page) / "ocr.json").read_text(encoding="utf-8"))


def _footer_label(pdf_page: int) -> str | None:
    path = rasterize.page_dir(pdf_page) / "footer.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8")).get("label")


def infer_labels(pages: list[int]) -> dict[int, str | None]:
    """Rótulo impresso ('6-3') por página, a partir de âncoras do OCR do rodapé.

    O OCR do rodapé confunde 6/0/O/G/J: a âncora vem preferencialmente da releitura só-dígitos
    (`ingest/footer.py`), e senão do OCR da página, só leituras estritas com confiança >= 80, e sempre
    com o prefixo da seção da página. O deslocamento (número - página do PDF) é local:
    algumas seções (11, 15, 23) têm páginas dobráveis/sem número e o deslocamento muda no meio.
    Âncoras isoladas (sem outra igual a até 8 páginas) são descartadas como erro de OCR. Página entre
    duas âncoras de deslocamentos diferentes fica sem rótulo (melhor nenhum que um errado). Fora do
    intervalo de âncoras, extrapola só se a seção tem um único deslocamento (ou até 3 páginas).
    """
    raw: dict[int, list[tuple[int, int]]] = {}
    for p in pages:
        sec = section_of(p)
        if not sec:      # None (antes do sumário) ou 0 (Special Information, sem numeração)
            continue
        label = _footer_label(p)          # releitura só do rodapé (dígitos), a mais confiável
        if label is None:                 # senão, o rodapé do OCR da página inteira
            for l in load_ocr(p)["lines"]:
                if l["region"] == "footer" and l["conf"] >= 80:
                    m = LABEL_STRICT.match(l["text"].strip().replace(" ", ""))
                    if m:
                        label = m.group(0)
        m = LABEL_STRICT.match(label or "")
        if m and int(m.group(1)) == sec:
            raw.setdefault(sec, []).append((p, int(m.group(2))))

    kept: dict[int, list[tuple[int, int]]] = {}
    for sec, lst in raw.items():
        lst = sorted(set(lst))
        if len(lst) <= 3:
            kept[sec] = lst
            continue
        kept[sec] = [(p, n) for i, (p, n) in enumerate(lst)
                     if any(j != i and n2 - p2 == n - p and abs(p2 - p) <= 8 for j, (p2, n2) in enumerate(lst))]

    out: dict[int, str | None] = {}
    for p in pages:
        sec = section_of(p)
        anchors = kept.get(sec or -1, [])
        if not sec or not anchors:
            out[p] = None
            continue
        exact = next((n for ap, n in anchors if ap == p), None)
        before = [a for a in anchors if a[0] < p]
        after = [a for a in anchors if a[0] > p]
        prev = before[-1] if before else None
        nxt = after[0] if after else None
        if exact is not None:
            n = exact
        elif prev and nxt and prev[1] - prev[0] != nxt[1] - nxt[0]:
            out[p] = None            # deslocamento muda entre as âncoras: ambíguo
            continue
        elif prev and nxt:
            n = p + prev[1] - prev[0]          # os dois lados concordam: interpola
        else:
            ref = prev or nxt
            uniform = len({n_ - p_ for p_, n_ in anchors}) == 1
            if not uniform and abs(ref[0] - p) > 3:   # deslocamento varia na seção: só extrapola perto
                out[p] = None
                continue
            n = p + ref[1] - ref[0]
        out[p] = f"{sec}-{n}" if n >= 1 else None
    return out


# ------------------------------------------------------------------ texto por página
@dataclass
class Block:
    kind: str        # 'text' | 'callout' | 'heading'
    text: str
    page: int
    col: str = ""
    y: int = 0


def page_blocks(pdf_page: int) -> list[Block]:
    """Texto normalizado, em ordem de leitura, separando corpo e callouts de figura."""
    o = load_ocr(pdf_page)
    out: list[Block] = []
    order = {"header": 0, "body": 1, "col1": 1, "col2": 2, "sparse": 3}
    for l in sorted((l for l in o["lines"] if l["region"] != "footer"),
                    key=lambda l: (order.get(l["region"], 9), l["blk"], l["y0"], l["x0"])):
        text = normalize(l["text"])
        if len(re.findall(r"[A-Za-z0-9]", text)) < 2:
            continue
        if l["conf"] < 55 and l["region"] != "header":
            continue
        kind = "callout" if l["in_figure"] else "text"
        out.append(Block(kind, text, pdf_page, l["region"], l["y0"]))
    return out


def looks_like_heading(text: str) -> bool:
    return len(text) < 90 and not text.endswith(".") and not re.match(r"^\d+\.", text)


# ------------------------------------------------------------------ procedimentos
@dataclass
class Procedure:
    toc_order: int
    title: str
    pdf_start: int
    pdf_end: int
    blocks: list[Block] = field(default_factory=list)


def load_toc() -> list[dict]:
    toc = json.loads(config.TOC_PATH.read_text(encoding="utf-8"))
    return sorted(toc, key=lambda e: (e["pdf_page"], e["order"]))


def _find_heading(blocks: list[Block], title: str) -> int:
    """Índice do bloco que parece ser o título do sumário (fuzzy), ou -1."""
    t = re.sub(r"\W+", " ", title).lower().strip()
    best, best_i = 0.0, -1
    for i, b in enumerate(blocks):
        if b.kind == "callout" or len(b.text) > len(title) + 25:
            continue
        r = difflib.SequenceMatcher(None, re.sub(r"\W+", " ", b.text).lower().strip(), t).ratio()
        if r > best:
            best, best_i = r, i
    return best_i if best >= 0.8 else -1


# ------------------------------------------------------------------ títulos de página (além do sumário)
CONTD_RE = re.compile(r"\(?\s*cont[’'`]?\s?d\s*\)?", re.I)


def _clean_heading(text: str) -> tuple[str, bool]:
    """Tira lixo de OCR e o '(cont'd)'. Retorna (título, era_continuação)."""
    t = re.sub(r"[—–_~=£|]{2,}.*$", "", text)                      # traços de moldura no fim
    contd = bool(CONTD_RE.search(t))
    t = CONTD_RE.sub("", t)
    t = re.sub(r"^[\W_]+", "", t)                                  # "— ", "~ ", "- " no início
    t = re.sub(r"^[a-zA-Z]\s+(?=[A-Z])", "", t)                    # letra solta ("r Relay Test")
    return re.sub(r"\s+", " ", t).strip(" -—–~:"), contd


def page_heading(pdf_page: int) -> tuple[str, str, bool, float, int] | None:
    """(título, subtítulo, continuação, confiança, altura da fonte) pelas linhas grandes do topo."""
    o = load_ocr(pdf_page)
    H = o["size"][1]
    cands = []
    for l in sorted(o["lines"], key=lambda l: l["y0"]):
        h = l["y1"] - l["y0"]
        text, contd = _clean_heading(l["text"])
        if (l["region"] != "footer" and 40 < l["y0"] < 0.13 * H and 44 <= h <= 120 and l["x0"] < 600
                and l["conf"] >= 45 and len(re.findall(r"[A-Za-z]", text)) >= 4
                and len(text.split()) <= 11 and not text.endswith(".")):
            cands.append((l, text, contd))
    if not cands:
        return None
    l0, title, contd = cands[0]
    sub = ""
    if len(cands) > 1 and cands[1][0]["y0"] - l0["y1"] < 130:
        sub, c2 = cands[1][1], cands[1][2]
        contd = contd or c2
    return title, sub, contd, l0["conf"], l0["y1"] - l0["y0"]


def _sim(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a.lower(), b.lower()).ratio()


def _canonical(title: str, seen: list[str]) -> str | None:
    """Título já visto que "é" este (o OCR trunca: 'ctor Identificatio' ~ 'Connector Identification...')."""
    tl = title.lower()
    for t in seen:
        if len(tl) >= 6 and (tl in t.lower() or _sim(title, t) >= 0.85):
            return t
    return None       # sem "parecido": 'Ignition System' e 'Ignition Switch' são títulos diferentes


def synthetic_entries(pages: list[int]) -> list[dict]:
    """Entradas de sumário para páginas que o sumário não cobre (elétrica, p. 936+): agrupa páginas
    seguidas pelo título do topo. Fonte grande = título; fonte menor sob um título já aberto = subtítulo
    dele. Título novo só vale com confiança >= 70; sem título legível a página continua a anterior."""
    out: list[dict] = []
    seen: list[str] = []
    cur_title, cur_sub = None, ""
    for p in sorted(pages):
        h = page_heading(p)
        if h is None and cur_title is not None:
            continue
        title, sub, _contd, conf, size = h if h else ("Electrical", "", False, 100.0, 70)
        if cur_title is None:
            t = title
        else:
            t = _canonical(title, seen)
            if t is None:                          # título nunca visto
                if size < 60 and not sub:          # linha única em fonte de subtítulo: é do título atual
                    sub, t = title, cur_title
                elif conf < 70:
                    continue
                else:
                    t = title
        if len(re.findall(r"[A-Za-z]", sub)) < 6:
            sub = ""
        if t == cur_title and (not sub or not cur_sub or _sim(sub, cur_sub) >= 0.8):
            if sub and not cur_sub:
                pass                               # subtítulo novo numa página de mesmo título: só continua
            continue
        cur_title, cur_sub = t, sub
        if t not in seen:
            seen.append(t)
        out.append({"order": 100000 + p, "title": f"{t} › {sub}" if sub else t,
                    "pdf_page": p, "synthetic": True})
    return out


def build_procedures(pages: list[int]) -> list[Procedure]:
    """Um procedimento por entrada do sumário: do título até o próximo título."""
    pset = set(pages)
    toc = load_toc()
    toc_last = max(e["pdf_page"] for e in toc)
    entries = [e for e in toc if e["pdf_page"] in pset and e["pdf_page"] >= config.FIRST_SCAN_PAGE]
    entries += synthetic_entries([p for p in pages if p > toc_last])     # o sumário acaba em toc_last
    entries.sort(key=lambda e: (e["pdf_page"], e["order"]))
    blocks_by_page = {p: page_blocks(p) for p in pages if p >= config.FIRST_SCAN_PAGE}
    # cada entrada começa no seu título (se achado) e termina no título da próxima
    cuts: list[tuple[int, int]] = []  # (página, índice de bloco)
    for e in entries:
        idx = 0 if e.get("synthetic") else _find_heading(blocks_by_page[e["pdf_page"]], e["title"])
        cuts.append((e["pdf_page"], max(idx, 0)))
    # se dois títulos da mesma página caem no mesmo bloco, o segundo vem depois do primeiro
    for i in range(1, len(cuts)):
        if cuts[i] <= cuts[i - 1] and cuts[i][0] == cuts[i - 1][0]:
            cuts[i] = (cuts[i][0], cuts[i - 1][1] + 1)
    procs = []
    last_page = max(pages)
    for i, e in enumerate(entries):
        start_p, start_i = cuts[i]
        if i + 1 < len(entries):
            end_p, end_i = cuts[i + 1]
        else:
            end_p, end_i = last_page + 1, 0
        blocks: list[Block] = []
        for p in range(start_p, min(end_p, last_page) + 1):
            bl = blocks_by_page.get(p, [])
            lo = start_i if p == start_p else 0
            hi = end_i if p == end_p else len(bl)
            blocks.extend(bl[lo:hi])
        last_real = end_p if end_i > 0 else end_p - 1
        procs.append(Procedure(e["order"], e["title"], start_p, max(start_p, min(last_real, last_page)), blocks))
    return procs


def procedure_type(title: str) -> str:
    t = title.lower()
    for key, val in OPS:
        if key in t:
            return val
    return "other"


ENGINE_RE = re.compile(r"\b(D15B7|D15B8|D15Z1|D16Z6)\b")


def applicability(text: str) -> dict:
    engines = sorted(set(ENGINE_RE.findall(text)))
    trans = []
    if re.search(r"\bM/T\b", text):
        trans.append("MT")
    if re.search(r"\bA/T\b", text):
        trans.append("AT")
    out: dict = {}
    if engines:
        out["engine"] = engines
    if trans:
        out["trans"] = trans
    return out


STEP_RE = re.compile(r"^(\d{1,2})\.\s+(.*)")


def extract_steps(blocks: list[Block]) -> list[dict]:
    steps: list[dict] = []
    for b in blocks:
        if b.kind != "text":
            continue
        m = STEP_RE.match(b.text)
        if m:
            steps.append({"n": int(m.group(1)), "text": m.group(2), "notes": [], "page": b.page})
        elif steps:
            if b.text.startswith("•"):
                steps[-1]["notes"].append(b.text.lstrip("• ").strip())
            elif not re.match(r"^[A-Z][A-Za-z/ ]{2,40}$", b.text) or b.text.islower():
                steps[-1]["text"] += " " + b.text
    return steps


WARN_RE = re.compile(r"^(CAUTION|WARNING|NOTE)\b[:\s]", re.I)


def extract_warnings(blocks: list[Block]) -> list[str]:
    out, cur = [], None
    for b in blocks:
        if b.kind != "text":
            continue
        if WARN_RE.match(b.text) or b.text.startswith("AWARNING") or b.text.startswith("ACAUTION"):
            cur = b.text
            out.append(cur)
        elif cur is not None and out and not STEP_RE.match(b.text) and not b.text.startswith("•") \
                and b.text[:1].islower():
            out[-1] += " " + b.text
        else:
            cur = None
    return out


CONSUMABLE_RE = re.compile(
    r"(?i)\b(replace[sd]?|new (?:o-rings?|gaskets?|seals?)|liquid gasket|engine oil|grease|"
    r"sealant|thread lock|coolant|atf)\b")


def extract_consumables(blocks: list[Block]) -> list[str]:
    seen, out = set(), []
    for b in blocks:
        if CONSUMABLE_RE.search(b.text) and len(b.text) < 160 and b.text not in seen:
            seen.add(b.text)
            out.append(b.text)
    return out[:12]
