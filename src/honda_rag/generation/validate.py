"""Validador pós-geração: todo número com unidade, código e citação da resposta deve existir no contexto."""
from __future__ import annotations

import re
import unicodedata

UNIT = r"(?:N·m|kg-m|lb-ft|mm|in|°C|°F|kPa|psi|rpm|V|A|Ω|ohms?|qt|L|cc|kg/cm2|kg/cm²|%)"
NUM = r"\d[\d,]*(?:\.\d+)?"
VALUE_RE = re.compile(rf"(?<![\w.])({NUM}(?:\s?[-–]\s?{NUM})?)\s?{UNIT}(?![\w])")
PART_RE = re.compile(r"\b\d{5}-\d{4}\b|\b\d{5}-[A-Z0-9]{3}-[A-Z0-9]{2,4}\b|\b07[A-Z0-9]{3}-[A-Z0-9]{6,7}\b")
ENGINE_RE = re.compile(r"\bD1\d[A-Z]\d\b")
CITE_RE = re.compile(r"\[p\.\s*([^\]]+)\]")
CODE_RE = re.compile(r"(?i)\bcode\s+(\d{1,2})\b|\bc[oó]digo\s+(\d{1,2})\b")
LABEL_RE = re.compile(r"\d{1,2}-\d{1,3}")


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKC", s).replace("–", "-").replace("—", "-")
    return re.sub(r"\s+", " ", s)


def _nums(tok: str) -> list[str]:
    return [n.replace(",", "") for n in re.findall(NUM, tok)]


def context_labels(ctx: str) -> set[str]:
    """Páginas presentes no contexto: 'p. 6-3, 6-4' nos prefixos de chunk e '[p. 6-3]' nas linhas de spec."""
    labels: set[str] = set()
    for m in re.finditer(r"p\.\s*((?:\d{1,2}-\d{1,3}(?:,\s*)?)+)", ctx):
        labels.update(LABEL_RE.findall(m.group(1)))
    return labels


def validate(answer: str, context: str, allowed_engines: list[str] | None = None) -> dict:
    """-> {'clean': resposta sem as linhas problemáticas, 'violations': [...], 'ok': bool}"""
    ctx = _norm(context)
    ctx_nums = {n.replace(",", "") for n in re.findall(NUM, ctx)}
    ctx_labels = context_labels(ctx)

    violations: list[dict] = []
    kept: list[str] = []
    for line in answer.split("\n"):
        nline = _norm(line)
        bad: list[str] = []
        for m in VALUE_RE.finditer(nline):
            if not all(n in ctx_nums for n in _nums(m.group(1))):
                bad.append(m.group(0))
        bad.extend(m.group(0) for m in PART_RE.finditer(nline) if m.group(0) not in ctx)
        # o motor do perfil do veículo pode ser citado mesmo que o contexto não o repita
        bad.extend(m.group(0) for m in ENGINE_RE.finditer(nline)
                   if m.group(0) not in ctx and m.group(0) not in (allowed_engines or []))
        for m in CODE_RE.finditer(nline):
            n = m.group(1) or m.group(2)
            if n not in ctx_nums:
                bad.append(m.group(0))
        if bad:
            violations.append({"line": line.strip(), "unverified": bad})
            continue
        kept.append(line)
    clean = "\n".join(kept)
    bad_cites: set[str] = set()
    for m in CITE_RE.finditer(clean):
        bad_cites.update(lab for lab in LABEL_RE.findall(m.group(1)) if lab not in ctx_labels)
    for lab in bad_cites:
        clean = re.sub(rf"\s?\[p\.\s*{re.escape(lab)}\]", "", clean)
    return {"clean": clean.strip(), "violations": violations, "bad_citations": sorted(bad_cites),
            "ok": not violations and not bad_cites}
