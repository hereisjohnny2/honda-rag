"""Acesso ao Postgres (psycopg 3)."""
from __future__ import annotations

import psycopg
from psycopg.types.json import Jsonb

from honda_rag import config


def connect() -> psycopg.Connection:
    return psycopg.connect(config.PG_DSN)


def vec(v: list[float]) -> str:
    return "[" + ",".join(f"{x:.6f}" for x in v) + "]"


def jb(x):
    return Jsonb(x) if x is not None else None


def reset_document(conn: psycopg.Connection, sha256: str) -> None:
    """Apaga o documento (cascade) para recarregar. Idempotente."""
    conn.execute("DELETE FROM documents WHERE file_sha256 = %s", (sha256,))
    conn.execute("DELETE FROM review_queue")  # a fila é recalculada a cada carga do piloto
