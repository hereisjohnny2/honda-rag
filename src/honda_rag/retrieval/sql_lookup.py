"""Busca exata/estruturada: specs, part numbers, ferramentas especiais, DTC."""
from __future__ import annotations

from honda_rag.db import repo

ENGINE_OK = ("(s.applicability->'engine' IS NULL OR jsonb_array_length(s.applicability->'engine') = 0 "
             "OR s.applicability->'engine' ? %(engine)s)")


def specs(component_terms: list[str], engine: str | None, kind: str = "none", limit: int = 12,
          side: str | None = None) -> list[dict]:
    """Specs cujo componente parece com algum termo (trigram), filtrando por motor."""
    terms = [t for t in component_terms if t]
    if not terms:
        return []
    kind_sql = ""
    if kind == "torque":
        kind_sql = "AND s.parameter = 'torque'"
    elif kind in ("clearance", "height", "limit"):
        kind_sql = "AND s.parameter <> 'torque'"
    if side:
        kind_sql += " AND (s.side = %(side)s OR s.side IS NULL)"
    sql = f"""
      SELECT s.id, s.component, s.parameter, s.value_text, s.fastener, s.side, s.conditions,
             s.applicability, p.page_label, p.pdf_page, s.verified,
             max(word_similarity(t.term, s.component || ' ' || coalesce(s.conditions,''))) AS score
        FROM specs s
        JOIN pages p ON p.id = s.page_id
        CROSS JOIN unnest(%(terms)s::text[]) AS t(term)
       WHERE {ENGINE_OK if engine else 'TRUE'} {kind_sql}
         AND word_similarity(t.term, s.component || ' ' || coalesce(s.conditions,'')) > 0.45
       GROUP BY s.id, p.page_label, p.pdf_page
       ORDER BY score DESC, s.id
       LIMIT %(limit)s"""
    with repo.connect() as conn:
        conn.row_factory = _dict_row
        return conn.execute(sql, {"terms": terms, "engine": engine, "limit": limit, "side": side}).fetchall()


def part_numbers(numbers: list[str], terms: list[str]) -> list[dict]:
    with repo.connect() as conn:
        conn.row_factory = _dict_row
        return conn.execute(
            """SELECT pn.part_number, pn.description, p.page_label, p.pdf_page
                 FROM part_numbers pn JOIN pages p ON p.id = pn.page_id
                WHERE pn.part_number = ANY(%s)
                   OR (pn.description IS NOT NULL AND pn.description ILIKE ANY(%s))""",
            (numbers, [f"%{t}%" for t in terms if t])).fetchall()


def special_tools(numbers: list[str], terms: list[str]) -> list[dict]:
    with repo.connect() as conn:
        conn.row_factory = _dict_row
        return conn.execute(
            """SELECT t.tool_number, t.name, p.page_label, p.pdf_page
                 FROM special_tools t LEFT JOIN pages p ON p.id = t.page_id
                WHERE t.tool_number = ANY(%s) OR t.name ILIKE ANY(%s)""",
            (numbers, [f"%{t}%" for t in terms if t])).fetchall()


def dtc(codes: list[str]) -> list[dict]:
    with repo.connect() as conn:
        conn.row_factory = _dict_row
        return conn.execute(
            """SELECT d.code, d.system, d.description, pr.title AS procedure, pr.page_labels
                 FROM dtc_codes d LEFT JOIN procedures pr ON pr.id = d.procedure_id
                WHERE d.code = ANY(%s)""", (codes,)).fetchall()


def _dict_row(cursor):
    from psycopg.rows import dict_row
    return dict_row(cursor)
