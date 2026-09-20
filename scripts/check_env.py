"""Verifica o ambiente da Fase 0: Tesseract, Ollama (+ modelos), Postgres/pgvector e PDF.

Uso:
    python scripts/check_env.py            # todas as checagens
    python scripts/check_env.py --no-ocr   # pula o teste de OCR na página 42
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

try:
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
except ImportError:
    pass

OK, FAIL, WARN = "[OK]    ", "[FALHA] ", "[AVISO] "
failures = 0


def report(ok: bool, msg: str, hint: str = "", warn_only: bool = False) -> None:
    global failures
    if ok:
        print(OK + msg)
        return
    print((WARN if warn_only else FAIL) + msg)
    if hint:
        print("         -> " + hint)
    if not warn_only:
        failures += 1


# --------------------------------------------------------------------------- Tesseract
def tesseract_cmd() -> str | None:
    cmd = os.getenv("TESSERACT_CMD", "").strip()
    if cmd and Path(cmd).exists():
        return cmd
    return shutil.which("tesseract")


def check_tesseract() -> str | None:
    cmd = tesseract_cmd()
    if not cmd:
        report(False, "Tesseract não encontrado",
               "Ajuste TESSERACT_CMD no .env ou adicione o Tesseract ao PATH")
        return None
    out = subprocess.run([cmd, "--version"], capture_output=True, text=True)
    version = (out.stdout or out.stderr).splitlines()[0]
    report(version.startswith("tesseract 5"), f"Tesseract: {version} ({cmd})",
           "Recomendado Tesseract 5.x", warn_only=True)
    langs = subprocess.run([cmd, "--list-langs"], capture_output=True, text=True).stdout
    report("eng" in langs.split(), "Tesseract: idioma 'eng' instalado",
           "Instale o pacote de idioma inglês (eng.traineddata)")
    return cmd


# --------------------------------------------------------------------------- Ollama
def ollama_get(host: str, path: str) -> dict:
    with urllib.request.urlopen(host + path, timeout=5) as r:
        return json.load(r)


def ollama_post(host: str, path: str, payload: dict, timeout: int = 120) -> dict:
    req = urllib.request.Request(host + path, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def check_ollama() -> None:
    host = os.getenv("OLLAMA_HOST", "http://localhost:11434").rstrip("/")
    try:
        ver = ollama_get(host, "/api/version").get("version")
        report(True, f"Ollama respondendo em {host} (versão {ver})")
    except (urllib.error.URLError, OSError) as e:
        report(False, f"Ollama não respondeu em {host}: {e}", "Abra o Ollama e tente de novo")
        return

    installed = {m["name"] for m in ollama_get(host, "/api/tags").get("models", [])}
    wanted = {
        "LLM": os.getenv("LLM_MODEL", "qwen3:8b"),
        "VLM": os.getenv("VLM_MODEL", "qwen2.5vl:7b"),
        "Embeddings": os.getenv("EMBED_MODEL", "bge-m3"),
    }
    for role, name in wanted.items():
        full = name if ":" in name else name + ":latest"
        report(full in installed or name in installed, f"Ollama modelo {role}: {name}",
               f"ollama pull {name}")

    embed = wanted["Embeddings"]
    if any(n.startswith(embed) for n in installed):
        try:
            res = ollama_post(host, "/api/embed", {"model": embed, "input": "cylinder head bolts torque"})
            dim = len(res["embeddings"][0])
            report(dim == 1024, f"Embedding de teste com {embed}: {dim} dimensões",
                   "O esquema usa VECTOR(1024); ajuste schema.sql se trocar de modelo")
        except Exception as e:  # noqa: BLE001
            report(False, f"Falha ao gerar embedding: {e}")

    for var, expected in [("OLLAMA_MAX_LOADED_MODELS", "1"), ("OLLAMA_FLASH_ATTENTION", "1")]:
        val = os.environ.get(var)
        report(val == expected, f"Variável {var}={val}",
               f"Defina {var}={expected} nas variáveis de ambiente do Windows e reinicie o Ollama "
               "(variável do servidor Ollama; este script só vê o ambiente do seu terminal)",
               warn_only=True)


# --------------------------------------------------------------------------- Postgres
def check_postgres() -> None:
    try:
        import psycopg
    except ImportError:
        report(False, "psycopg não instalado", "pip install -e .")
        return
    dsn = (f"host={os.getenv('PG_HOST', 'localhost')} port={os.getenv('PG_PORT', '5432')} "
           f"dbname={os.getenv('PG_DB', 'honda_rag')} user={os.getenv('PG_USER', 'honda')} "
           f"password={os.getenv('PG_PASSWORD', 'honda')}")
    try:
        with psycopg.connect(dsn, connect_timeout=5) as conn:
            v = conn.execute("show server_version").fetchone()[0]
            report(True, f"Postgres conectado (versão {v})")
            exts = {r[0] for r in conn.execute("select extname from pg_extension")}
            report("vector" in exts, "Extensão pgvector ativa")
            report("pg_trgm" in exts, "Extensão pg_trgm ativa")
            n = conn.execute("select count(*) from information_schema.tables "
                             "where table_schema='public'").fetchone()[0]
            report(n >= 15, f"Esquema criado ({n} tabelas)",
                   "O schema.sql só roda com volume vazio: docker compose down -v && docker compose up -d")
            if "vector" in exts:
                d = conn.execute("select '[1,0,0]'::vector <=> '[0,1,0]'::vector").fetchone()[0]
                report(abs(d - 1.0) < 1e-6, "Operador de distância do pgvector funcionando")
    except Exception as e:  # noqa: BLE001
        report(False, f"Postgres indisponível: {e}",
               "Rode 'docker compose up -d' (ou 'nerdctl compose up -d') na raiz do projeto")


# --------------------------------------------------------------------------- PDF + OCR
def check_pdf_and_ocr(tess: str | None, run_ocr: bool) -> None:
    pdf = ROOT / os.getenv("PDF_PATH", "data/raw/civic_1992-1995_service_manual.pdf")
    if not pdf.exists():
        report(False, f"PDF não encontrado: {pdf}")
        return
    try:
        import fitz  # PyMuPDF
    except ImportError:
        report(False, "PyMuPDF não instalado", "pip install -e .")
        return
    doc = fitz.open(pdf)
    report(doc.page_count == 1458, f"PDF aberto: {doc.page_count} páginas", warn_only=True)
    toc = ROOT / "data/meta/toc_links.json"
    report(toc.exists(), "Mapa do sumário: data/meta/toc_links.json", warn_only=True)

    if not (run_ocr and tess):
        return
    # Página 42 do PDF = 3-2 (Standards and Service Limits, Cylinder Head/Valve Train)
    pix = doc[41].get_pixmap(dpi=300, colorspace=fitz.csGRAY)
    res = subprocess.run([tess, "stdin", "stdout", "--psm", "6"], input=pix.tobytes("png"),
                         capture_output=True)
    text = res.stdout.decode("utf-8", errors="replace")
    expected = ["92.95", "0.18", "0.23", "D16Z6", "Service"]
    found = [e for e in expected if e in text]
    report(len(found) == len(expected),
           f"OCR de teste na p. 42 (3-2): {len(found)}/{len(expected)} valores esperados encontrados",
           f"Faltando: {sorted(set(expected) - set(found))}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-ocr", action="store_true", help="pula o teste de OCR")
    args = ap.parse_args()

    print("== Tesseract"); tess = check_tesseract()
    print("\n== Ollama"); check_ollama()
    print("\n== Postgres"); check_postgres()
    print("\n== PDF / OCR"); check_pdf_and_ocr(tess, not args.no_ocr)

    print("\n" + ("Tudo pronto para a Fase 0." if failures == 0 else f"{failures} item(ns) com falha."))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
