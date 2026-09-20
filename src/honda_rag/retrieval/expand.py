"""Expansão: chunk filho -> contexto para o LLM (limite de tamanho), com specs e figuras."""
from __future__ import annotations

from psycopg.rows import dict_row

from honda_rag import config
from honda_rag.db import repo



def build_context(hits: list[dict], spec_rows: list[dict], extra_rows: list[str],
                  max_chunks: int | None = None) -> tuple[str, list[dict]]:
    """Monta o CONTEXTO. Retorna (texto, chunks usados)."""
    parts: list[str] = []
    used: list[dict] = []
    max_chunks = max_chunks or config.CONTEXT_CHUNKS
    budget = config.CONTEXT_CHARS
    for line in extra_rows:
        parts.append(line)
        budget -= len(line)
    if spec_rows:
        block = ["[SPEC TABLE — exact values from the manual]"]
        for s in spec_rows:
            engines = (s["applicability"] or {}).get("engine", [])
            eng = f" [{', '.join(engines)}]" if engines else ""
            side = f" {s['side']}" if s.get("side") else ""
            fast = f" (fastener {s['fastener']})" if s.get("fastener") else ""
            cond = f" ({s['conditions']})" if s.get("conditions") else ""
            block.append(f"- {s['component']}{side}{fast}{cond}{eng} — {s['parameter']}: {s['value_text']} "
                         f"[p. {s['page_label'] or 'PDF ' + str(s['pdf_page'])}]")
        txt = "\n".join(block)
        parts.append(txt)
        budget -= len(txt)
    seen_proc: dict[int, int] = {}
    for h in hits:
        if len(used) >= max_chunks or budget <= 300:
            break
        pid = h["procedure_id"]
        if pid is not None and seen_proc.get(pid, 0) >= 2:
            continue
        text = h["text"][:budget]
        parts.append(text)
        budget -= len(text)
        seen_proc[pid] = seen_proc.get(pid, 0) + 1
        used.append(h)
    return "\n\n".join(parts), used


def matching_steps(procedure_ids: list[int], phrases: list[str], limit: int = 4) -> list[dict]:
    """Passos originais (EN) dos procedimentos recuperados cujo texto contém TODAS as palavras de alguma
    frase do componente (ex.: 'P/S pump' casa 'Remove the P/S belt and pump'). Determinístico: sem LLM."""
    import re
    toks = [[t for t in re.findall(r"[a-z0-9/]+", ph.lower()) if len(t) >= 3 or "/" in t] for ph in phrases]
    toks = [t for t in toks if t]
    if not toks or not procedure_ids:
        return []
    with repo.connect() as conn:
        conn.row_factory = dict_row
        procs = conn.execute("SELECT id, title, steps FROM procedures WHERE id = ANY(%s)", (procedure_ids,)).fetchall()
        labels = {r["pdf_page"]: r["page_label"] for r in conn.execute("SELECT pdf_page, page_label FROM pages")}
    out = []
    for pr in procs:
        for st in pr["steps"] or []:
            blob = (st["text"] + " " + " ".join(st.get("notes", []))).lower()
            if any(all(t in blob for t in ph) for ph in toks):
                out.append({"n": st["n"], "text": st["text"], "notes": st.get("notes", []),
                            "label": labels.get(st.get("page")), "procedure": pr["title"]})
    out.sort(key=lambda x: (x["procedure"], x["n"]))
    return out[:limit]


def figures_for(page_labels: list[str], limit: int = 6) -> list[dict]:
    if not page_labels:
        return []
    with repo.connect() as conn:
        conn.row_factory = dict_row
        return conn.execute(
            """SELECT f.id, f.image_path, f.figure_type, f.bbox, p.page_label, p.pdf_page, p.view_path
                 FROM figures f JOIN pages p ON p.id = f.page_id
                WHERE p.page_label = ANY(%s) ORDER BY p.pdf_page, f.id LIMIT %s""",
            (page_labels, limit)).fetchall()
