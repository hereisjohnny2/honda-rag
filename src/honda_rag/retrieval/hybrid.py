"""Busca híbrida: pgvector (cosseno) + full-text Postgres, fundidos por RRF."""
from __future__ import annotations

from psycopg.rows import dict_row

from honda_rag import llm
from honda_rag.db import repo

RRF_K = 60
ENGINE_OK = ("(c.applicability->'engine' IS NULL OR jsonb_array_length(c.applicability->'engine') = 0 "
             "OR c.applicability->'engine' ? %(engine)s)")


def search(queries_en: list[str], question_pt: str, engine: str | None, k: int = 20,
           types: list[str] | None = None) -> list[dict]:
    """Retorna chunks ordenados por RRF, cada um com 'score', 'cos' (melhor similaridade) e 'ranks'."""
    vectors = llm.embed(queries_en + [question_pt])
    flt = ENGINE_OK if engine else "TRUE"
    if types:
        flt += " AND c.chunk_type = ANY(%(types)s)"
    ranks: dict[int, dict] = {}
    best_cos: dict[int, float] = {}
    with repo.connect() as conn:
        conn.row_factory = dict_row
        for qi, v in enumerate(vectors):
            rows = conn.execute(
                f"""SELECT c.id, 1 - (c.embedding <=> %(v)s::vector) AS cos
                      FROM chunks c WHERE c.embedding IS NOT NULL AND {flt}
                     ORDER BY c.embedding <=> %(v)s::vector LIMIT %(k)s""",
                {"v": repo.vec(v), "engine": engine, "k": k, "types": types}).fetchall()
            for i, r in enumerate(rows):
                ranks.setdefault(r["id"], {})[f"v{qi}"] = i + 1
                best_cos[r["id"]] = max(best_cos.get(r["id"], 0), float(r["cos"]))
        for j, q in enumerate(queries_en):
            rows = conn.execute(
                f"""SELECT c.id, ts_rank_cd(c.tsv, websearch_to_tsquery('english', %(q)s)) AS r
                      FROM chunks c
                     WHERE c.tsv @@ websearch_to_tsquery('english', %(q)s) AND {flt}
                     ORDER BY r DESC LIMIT %(k)s""",
                {"q": q, "engine": engine, "k": k, "types": types}).fetchall()
            for i, r in enumerate(rows):
                ranks.setdefault(r["id"], {})["f%d" % j] = i + 1
        # OR lexical amplo (websearch usa AND): termos soltos da primeira consulta
        terms = " OR ".join(w for w in queries_en[0].split() if len(w) > 2)
        if terms:
            rows = conn.execute(
                f"""SELECT c.id, ts_rank_cd(c.tsv, websearch_to_tsquery('english', %(q)s)) AS r
                      FROM chunks c
                     WHERE c.tsv @@ websearch_to_tsquery('english', %(q)s) AND {flt}
                     ORDER BY r DESC LIMIT %(k)s""",
                {"q": terms, "engine": engine, "k": k, "types": types}).fetchall()
            for i, r in enumerate(rows):
                ranks.setdefault(r["id"], {})["fo"] = i + 1
        scored = {cid: sum(1.0 / (RRF_K + rk) for rk in rk_d.values()) for cid, rk_d in ranks.items()}
        top = sorted(scored, key=scored.get, reverse=True)[:k]
        if not top:
            return []
        rows = conn.execute(
            """SELECT c.id, c.procedure_id, c.chunk_type, c.text, c.page_labels, c.applicability,
                      pr.title AS procedure_title
                 FROM chunks c LEFT JOIN procedures pr ON pr.id = c.procedure_id WHERE c.id = ANY(%s)""",
            (top,)).fetchall()
    by_id = {r["id"]: r for r in rows}
    return [dict(by_id[cid], score=scored[cid], cos=best_cos.get(cid, 0.0), ranks=ranks[cid]) for cid in top]
