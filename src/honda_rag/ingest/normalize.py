"""Normalização de erros sistemáticos do OCR neste manual (PLAN §4.4).

Regras com contexto (números ao redor), nunca substituições cegas.
"""
from __future__ import annotations

import re

from honda_rag.config import ENGINES

_RULES: list[tuple[re.Pattern, str]] = [
    # N·m: "22 Nem", "22 N*m", "22 N-m", "22 Nm", "22 N.m", "22 N•m"
    (re.compile(r"(?<=\d)\s*N\s?[e*\-.•·°o]\s?m\b"), " N·m"),
    (re.compile(r"(?<=\d)\s*Nm\b"), " N·m"),
    (re.compile(r"(?<=\d)\s*N·\s?m\b"), " N·m"),
    # lb-ft
    (re.compile(r"(?<=\d)\s*[IilW1|]b\s?[-—–]\s?ft\b"), " lb-ft"),
    (re.compile(r"(?<=\d)\s*lb\s?[-—–]\s?ft\b"), " lb-ft"),
    # kg-m
    (re.compile(r"(?<=\d)\s*kg\s?[-—–~]\s?(?:m|rn)\b"), " kg-m"),
    # travessões entre números -> faixa
    (re.compile(r"(?<=\d)\s?[—–�~]\s?(?=\d)"), "-"),
    # marcador de item
    (re.compile(r"^\s*[@�©®]\s+"), "• "),
    # dimensões: 8x 1.25 mm -> 8 x 1.25 mm
    (re.compile(r"\b(\d{1,2})\s?[xX×]\s?(\d\.\d{1,2}) ?mm\b"), r"\1 x \2 mm"),
    (re.compile(r"(\d)\s?°\s?C\b"), r"\1 °C"),
    (re.compile(r"(?<=\d)�\s?([CF])\b"), r" °\1"),
]

# lista fechada de códigos de motor + confusões típicas de OCR
_ENGINE_FIX = {
    "D1SB7": "D15B7", "D1SB8": "D15B8", "D1i5B8": "D15B8", "D15BB": "D15B8",
    "D1SZ1": "D15Z1", "D15ZI": "D15Z1", "D1SZ1": "D15Z1",
    "DI6Z6": "D16Z6", "Dl6Z6": "D16Z6", "D16Z8": "D16Z6", "D162Z6": "D16Z6",
}
_ENGINE_LIKE = re.compile(r"\bD\s?[1Il]\s?[5S6]\s?[A-Z1-9]\s?\d\b")


def normalize_engine(tok: str) -> str:
    t = tok.replace(" ", "")
    if t in ENGINES:
        return t
    if t in _ENGINE_FIX:
        return _ENGINE_FIX[t]
    # OCR trocou um caractere: só corrige se houver exatamente um motor a 1 de distância
    near = [e for e in ENGINES if len(e) == len(t) and sum(a != b for a, b in zip(e, t)) == 1]
    return near[0] if len(near) == 1 else t


def normalize(text: str) -> str:
    for pat, rep in _RULES:
        text = pat.sub(rep, text)
    text = _ENGINE_LIKE.sub(lambda m: normalize_engine(m.group(0)), text)
    for bad, good in _ENGINE_FIX.items():
        text = text.replace(bad, good)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text.strip()
