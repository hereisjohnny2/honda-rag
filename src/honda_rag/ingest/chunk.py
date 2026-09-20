"""Etapa 5: texto do procedimento (pai) e chunks (filhos) com prefixo de contexto."""
from __future__ import annotations

import re

from honda_rag.ingest.structure import STEP_RE, WARN_RE, Block, looks_like_heading

MAX_CHARS = 1700   # ~400 tokens


def paragraphs(blocks: list[Block]) -> list[tuple[int, str]]:
    """Linhas de OCR -> parágrafos (página, texto). Passos, marcadores e avisos abrem parágrafo."""
    paras: list[tuple[int, str]] = []
    for b in blocks:
        if b.kind != "text":
            continue
        t = b.text
        starts_new = (not paras or STEP_RE.match(t) or t.startswith("•") or WARN_RE.match(t)
                      or looks_like_heading(t) and t[:1].isupper() and b.col != "col2"
                      or paras[-1][1].endswith((".", ":", ")")) and t[:1].isupper())
        if starts_new or paras[-1][0] != b.page:
            paras.append((b.page, t))
        else:
            prev = paras[-1][1]
            joiner = "" if prev.endswith("-") and t[:1].islower() else " "
            paras[-1] = (b.page, (prev[:-1] if joiner == "" else prev) + joiner + t)
    return paras


ENGINE_HEAD = re.compile(r"^(D\w{4}(,\s*)?)+\s+engine", re.I)


def _clean_callout(t: str) -> str | None:
    t = re.sub(r"^[^\w(•]+|[\|>~_]+\s*$", "", t).strip()
    # descarta traço de desenho lido como texto ("Ss", "YO >", "SF."): sem palavra de 3+ letras nem número
    if not (re.search(r"[A-Za-z]{3,}", t) or re.search(r"\d", t)):
        return None
    return t


def callout_texts(blocks: list[Block]) -> dict[int, list[str]]:
    out: dict[int, list[str]] = {}
    for b in blocks:
        if b.kind != "callout":
            continue
        t = _clean_callout(b.text)
        lst = out.setdefault(b.page, [])
        if t and not any(prev.endswith(t) for prev in lst[-2:]):  # "17 lb-ft)" repetido do modo esparso
            lst.append(t)
    return out


def merge_callouts(lines: list[str]) -> str:
    """Callouts vêm quebrados em linhas curtas; junta as que formam a mesma frase de rótulo."""
    return " | ".join(lines)


def procedure_full_text(blocks: list[Block]) -> str:
    parts = [p for _, p in paragraphs(blocks)]
    for page, lines in callout_texts(blocks).items():
        parts.append(f"[Figure callouts, PDF p. {page}] " + merge_callouts(lines))
    return "\n".join(parts)


def make_prefix(section: str | None, title: str, engines: list[str], labels: list[str]) -> str:
    bits = [b for b in (section, title, "/".join(engines) if engines else None,
                        ("p. " + ", ".join(labels)) if labels else None) if b]
    return "[" + " · ".join(bits) + "]"


def split_chunks(blocks: list[Block], page_label: dict[int, str | None], prefix_fn) -> list[dict]:
    """Filhos de ~400 tokens sem quebrar parágrafos. Retorna dicts {text, pages, labels}."""
    units: list[tuple[int, str]] = list(paragraphs(blocks))
    for page, lines in callout_texts(blocks).items():
        units.append((page, f"[Figure callouts] " + merge_callouts(lines)))
    units.sort(key=lambda u: u[0])
    chunks, cur, cur_pages = [], [], []
    size = 0
    for page, text in units:
        if cur and (size + len(text) > MAX_CHARS or ENGINE_HEAD.match(text)):
            chunks.append((cur, cur_pages))
            cur, cur_pages, size = [], [], 0
        cur.append(text)
        cur_pages.append(page)
        size += len(text)
    if cur:
        chunks.append((cur, cur_pages))
    out = []
    for texts, pages in chunks:
        labels = sorted({page_label.get(p) for p in pages if page_label.get(p)},
                        key=lambda s: tuple(int(x) for x in s.split("-")))
        body = "\n".join(texts)
        out.append({"text": prefix_fn(labels) + "\n" + body, "pages": sorted(set(pages)),
                    "labels": labels, "body": body})
    return out


def spec_chunk_texts(specs: list[dict], prefix: str, limit: int = MAX_CHARS) -> list[str]:
    """Um chunk por grupo de linhas da tabela (mesmo componente/página)."""
    def line(s: dict) -> str:
        eng = f" [{', '.join(s['engines'])}]" if s.get("engines") else ""
        side = f" {s['side']}" if s.get("side") else ""
        cond = (s.get("conditions") or "").replace("Cylinder Head/Valve Train Section 6", "").strip()
        cond = f" ({cond})" if cond else ""
        fast = f" ({s['fastener']})" if s.get("fastener") and s["fastener"] not in s["component"] else ""
        return f"{s['component']}{fast}{side}{cond}{eng} — {s['parameter'].replace('_', ' ')}: {s['value_text']}"

    out, cur, size = [], [], 0
    for s in specs:
        ln = line(s)
        if cur and size + len(ln) > limit:
            out.append(prefix + "\n" + "\n".join(cur))
            cur, size = [], 0
        cur.append(ln)
        size += len(ln)
    if cur:
        out.append(prefix + "\n" + "\n".join(cur))
    return out
