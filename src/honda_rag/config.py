"""Configuração central (lida do .env)."""
from __future__ import annotations

import os
import shutil
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
load_dotenv(ROOT / ".env")


def _pages(spec: str) -> list[int]:
    """'1-110,120' -> [1..110, 120]"""
    out: list[int] = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-")
            out.extend(range(int(a), int(b) + 1))
        else:
            out.append(int(part))
    return out


PDF_PATH = ROOT / os.getenv("PDF_PATH", "data/raw/civic_1992-1995_service_manual.pdf")
DATA_DIR = ROOT / "data"
PAGES_DIR = DATA_DIR / "pages"
FIGURES_DIR = DATA_DIR / "figures"
TOC_PATH = DATA_DIR / "meta" / "toc_links.json"
PILOT_PAGES = _pages(os.getenv("PILOT_PAGES", "1-110"))
FIRST_SCAN_PAGE = 25  # páginas 1-24 são o sumário do ManualsLib (texto nativo)

OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://localhost:11434").rstrip("/")
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "ollama").strip().lower()   # ollama | claude | gemini
LLM_MODEL = os.getenv("LLM_MODEL", "qwen3:8b")                       # modelo do Ollama
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-haiku-4-5")          # usado se LLM_PROVIDER=claude
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")          # usado se LLM_PROVIDER=gemini
# Orçamento de "thinking" do Gemini: 0 desliga (só modelos flash aceitam 0; para "pro" fica vazio).
GEMINI_THINKING_BUDGET = os.getenv("GEMINI_THINKING_BUDGET", "" if "pro" in GEMINI_MODEL.lower() else "0")
USE_SYSTEM_CERTS = os.getenv("USE_SYSTEM_CERTS", "1") != "0"   # certificados do Windows (proxy corporativo)
_API_PROVIDER = LLM_PROVIDER in ("claude", "gemini")
# Contexto da resposta (caracteres). O 8B local tem 8k tokens; as APIs aguentam muito mais.
CONTEXT_CHARS = int(os.getenv("CONTEXT_CHARS", "24000" if _API_PROVIDER else "9000"))
CONTEXT_CHUNKS = int(os.getenv("CONTEXT_CHUNKS", "8" if _API_PROVIDER else "5"))
VLM_MODEL = os.getenv("VLM_MODEL", "qwen2.5vl:7b")
EMBED_MODEL = os.getenv("EMBED_MODEL", "bge-m3")

TESSERACT_CMD = os.getenv("TESSERACT_CMD", "").strip() or shutil.which("tesseract") or "tesseract"

PG_DSN = (
    f"host={os.getenv('PG_HOST', 'localhost')} port={os.getenv('PG_PORT', '5432')} "
    f"dbname={os.getenv('PG_DB', 'honda_rag')} user={os.getenv('PG_USER', 'honda')} "
    f"password={os.getenv('PG_PASSWORD', 'honda')}"
)

ENGINES = ["D15B7", "D15B8", "D15Z1", "D16Z6"]
DEFAULT_ENGINE = "D16Z6"
