"""Intenção + entidades + tradução/expansão PT->EN (uma chamada de LLM, com fallback por glossário)."""
from __future__ import annotations

import re
import unicodedata

from honda_rag import config, llm
from honda_rag.db import repo

INTENTS = ["procedure", "spec", "part_number", "special_tool", "diagram", "troubleshooting", "dtc", "general"]

SYSTEM = """You help search a Honda Civic 1992-1995 service manual (written in English).
The mechanic asks in Brazilian Portuguese. Return ONLY JSON with keys:
  "intent": one of %s
     (spec = torque/clearance/limit/dimension value; procedure = how to do a job;
      troubleshooting = symptom or diagnostic flow; dtc = Check Engine light blink code;
      part_number = part/product number; special_tool = special tool; diagram = wants a figure)
  "component_en": the main component in manual terminology, e.g. "cylinder head bolts"
  "operation": one of removal, installation, inspection, adjustment, replacement, none
  "spec_kind": one of torque, clearance, height, limit, other, none
  "queries_en": 2-3 short English search queries using the manual's own wording
  "codes": list of literal codes in the question (engine codes, part numbers, blink codes)
Use the glossary hints when they apply."""


def _strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn")


def glossary_hits(question: str) -> dict[str, list[str]]:
    q = _strip_accents(question.lower())
    with repo.connect() as conn:
        rows = conn.execute("SELECT term_pt, term_en FROM glossary_pt_en").fetchall()
    hits = {pt: en for pt, en in rows if _strip_accents(pt.lower()) in q}
    # termos longos primeiro; remove os contidos em outro (cabeçote dentro de "parafusos do cabeçote")
    keys = sorted(hits, key=len, reverse=True)
    return {k: hits[k] for i, k in enumerate(keys)
            if not any(_strip_accents(k.lower()) in _strip_accents(o.lower()) for o in keys[:i])}


def regex_codes(question: str) -> dict:
    return {
        "engines": sorted(set(re.findall(r"\bD\s?1\d\s?[A-Z]\s?\d\b", question.upper().replace(" ", "")))),
        "part_numbers": re.findall(r"\b\d{5}-\d{4}\b|\b\d{5}-[A-Z0-9]{3}-[A-Z0-9]{2,4}\b", question.upper()),
        "tool_numbers": re.findall(r"\b07[A-Z0-9]{3}-[A-Z0-9]{6,7}\b", question.upper()),
        "blink": re.findall(r"(?i)\b(?:c[oó]digo|code)\s*(\d{1,2})\b", question),
    }


def detect_side(question: str) -> str | None:
    """Admissão (IN) x escape (EX): decisão determinística, não deixa para o LLM escolher a linha da tabela."""
    q = _strip_accents(question.lower())
    intake = bool(re.search(r"admiss|intake|in", q))
    exhaust = bool(re.search(r"escape|exhaust|ex", q))
    return "IN" if intake and not exhaust else "EX" if exhaust and not intake else None


def analyze(question: str) -> dict:
    hits = glossary_hits(question)
    codes = regex_codes(question)
    hint = "; ".join(f"{pt} = {' / '.join(en)}" for pt, en in hits.items()) or "(none)"
    out = llm.chat_json([
        {"role": "system", "content": SYSTEM % INTENTS},
        {"role": "user", "content": f"Glossary hints: {hint}\nQuestion: {question}"},
    ], temperature=0.0)
    intent = out.get("intent") if out.get("intent") in INTENTS else "general"
    queries = [q for q in out.get("queries_en", []) if isinstance(q, str) and q.strip()][:3]
    # fallback / reforço: termos do glossário sempre entram
    gloss_q = " ".join(en[0] for en in hits.values())
    if gloss_q and gloss_q not in queries:
        queries.append(gloss_q)
    for en in hits.values():        # sinônimos/abreviações do manual ("P/S pump") viram consultas próprias
        for alt in en[1:3]:
            if alt not in queries and len(alt) > 3:
                queries.append(alt)
    queries = queries[:6]
    if codes["blink"] and intent in ("general", "troubleshooting"):
        intent = "dtc"
    if codes["part_numbers"]:
        intent = "part_number"
    if codes["tool_numbers"]:
        intent = "special_tool"
    return {
        "intent": intent, "component_en": (out.get("component_en") or "").strip(),
        "operation": out.get("operation", "none"), "spec_kind": out.get("spec_kind", "none"),
        "queries_en": queries or [question], "codes": codes, "glossary": hits,
        "side": detect_side(question),
    }
