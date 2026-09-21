"""Configuração central (lida do .env)."""
from __future__ import annotations

import os
import shutil
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
load_dotenv(ROOT / ".env")


def resolve(rel: str) -> Path:
    """Caminho gravado no banco (relativo à raiz do projeto) -> Path absoluto. Aceita barras invertidas:
    bancos carregados no Windows guardam `data\\pages\\42\\view.webp`, que no Linux seria um nome de arquivo."""
    return ROOT / rel.replace("\\", "/")


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
# Provedor de chat PADRÃO (ollama | claude | gemini | grok | hf). A UI e a CLI podem sobrepor por
# sessão/chamada (honda_rag.llm.use); quem não mexe em nada usa este valor. Lista completa e metadados
# de cada provedor (chave, modelo padrão, URL) ficam em honda_rag.providers.
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "ollama").strip().lower()
LLM_MODEL = os.getenv("LLM_MODEL", "qwen3:8b")                       # modelo do Ollama
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-haiku-4-5")          # usado se o provedor for claude
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")          # usado se o provedor for gemini
GROK_MODEL = os.getenv("GROK_MODEL", "grok-4-fast")                   # usado se o provedor for grok
HF_MODEL = os.getenv("HF_MODEL", "meta-llama/Llama-3.3-70B-Instruct")  # usado se o provedor for hf
# Grok e Hugging Face falam a API de chat no formato da OpenAI (POST {base}/chat/completions).
XAI_BASE_URL = os.getenv("XAI_BASE_URL", "https://api.x.ai/v1").rstrip("/")
HF_BASE_URL = os.getenv("HF_BASE_URL", "https://router.huggingface.co/v1").rstrip("/")
# Orçamento de "thinking" do Gemini: 0 desliga (só modelos flash aceitam 0; para "pro" fica vazio).
GEMINI_THINKING_BUDGET = os.getenv("GEMINI_THINKING_BUDGET", "" if "pro" in GEMINI_MODEL.lower() else "0")
USE_SYSTEM_CERTS = os.getenv("USE_SYSTEM_CERTS", "1") != "0"   # certificados do Windows (proxy corporativo)
_CONTEXT_CHARS_ENV = os.getenv("CONTEXT_CHARS", "").strip()
_CONTEXT_CHUNKS_ENV = os.getenv("CONTEXT_CHUNKS", "").strip()


def context_budget(kind: str) -> tuple[int, int]:
    """(CONTEXT_CHARS, CONTEXT_CHUNKS) para um provedor de orçamento `kind` ("local" ou "api";
    honda_rag.providers.Provider.budget). O .env, quando define CONTEXT_CHARS/CONTEXT_CHUNKS, sempre
    vence; senão 24.000/8 para provedores de API e 9.000/5 para o 8B local — o local tem 8k tokens de
    contexto, as APIs aguentam muito mais. Função (não constante) porque o provedor pode mudar por
    sessão/chamada (honda_rag.llm.use), depois do import deste módulo."""
    d_chars, d_chunks = (24000, 8) if kind == "api" else (9000, 5)
    chars = int(_CONTEXT_CHARS_ENV) if _CONTEXT_CHARS_ENV else d_chars
    chunks = int(_CONTEXT_CHUNKS_ENV) if _CONTEXT_CHUNKS_ENV else d_chunks
    return chars, chunks


VLM_MODEL = os.getenv("VLM_MODEL", "qwen2.5vl:7b")
EMBED_MODEL = os.getenv("EMBED_MODEL", "bge-m3")                      # modelo do Ollama
# Embeddings: "ollama" (bge-m3, local) ou "gemini" (API). Os vetores do banco e os da pergunta precisam
# vir do MESMO modelo: trocar o provedor exige `--stage embed` (recalcula todos os chunks).
EMBED_PROVIDER = os.getenv("EMBED_PROVIDER", "ollama").strip().lower()
GEMINI_EMBED_MODEL = os.getenv("GEMINI_EMBED_MODEL", "gemini-embedding-001")
# Similaridade de cosseno mínima do melhor chunk para responder (senão recusa). Depende do modelo de
# embedding: 0.55 foi calibrado com o bge-m3. Ao trocar o modelo, recalibre com eval/run_retrieval.py.
MIN_COSINE = float(os.getenv("MIN_COSINE", "0.55"))
# Quanto tempo o Ollama mantém o bge-m3 na memória entre perguntas. No servidor, use "24h": recarregar o
# modelo (~1,2 GB) a cada pergunta custa vários segundos numa CPU pequena.
EMBED_KEEP_ALIVE = os.getenv("EMBED_KEEP_ALIVE", "10m")
EMBED_DIM = 1024       # = VECTOR(1024) em db/schema.sql; o gemini-embedding-001 aceita reduzir a dimensão
# identificador gravado em chunks.embedding_model (o bge-m3 mantém o nome antigo, sem reembutir)
EMBED_ID = EMBED_MODEL if EMBED_PROVIDER == "ollama" else f"gemini:{GEMINI_EMBED_MODEL}:{EMBED_DIM}"

TESSERACT_CMD = os.getenv("TESSERACT_CMD", "").strip() or shutil.which("tesseract") or "tesseract"

PG_DSN = (
    f"host={os.getenv('PG_HOST', 'localhost')} port={os.getenv('PG_PORT', '5432')} "
    f"dbname={os.getenv('PG_DB', 'honda_rag')} user={os.getenv('PG_USER', 'honda')} "
    f"password={os.getenv('PG_PASSWORD', 'honda')}"
)

ENGINES = ["D15B7", "D15B8", "D15Z1", "D16Z6"]
DEFAULT_ENGINE = "D16Z6"
