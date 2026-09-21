"""Pipeline RAG completo: pergunta -> intenção -> SQL/híbrida -> contexto -> LLM -> validação."""
from __future__ import annotations

import re
import time

from honda_rag import config, llm
from honda_rag.generation import prompts, validate as V
from honda_rag.retrieval import expand, hybrid, intent as I, sql_lookup as SQL

CHUNK_TYPES = {"spec": ["spec_table", "procedure", "flowchart"],
               "procedure": ["procedure", "flowchart", "text"],
               "troubleshooting": ["procedure", "flowchart", "text"],
               "diagram": ["procedure", "flowchart", "text"]}
ENGINE_LIKE = re.compile(r"\bD\s?1\d\s?[A-Z]\s?\d\b")


def _engines_out_of_scope(question: str) -> list[str]:
    found = {m.replace(" ", "").upper() for m in ENGINE_LIKE.findall(question.upper())}
    return sorted(e for e in found if e not in config.ENGINES)


def answer(question: str, engine: str | None = config.DEFAULT_ENGINE, trans: str | None = None,
           debug: bool = False, choice: llm.Choice | None = None) -> dict:
    """`choice` sobrepõe o provedor de LLM (padrão: LLM_PROVIDER do .env) só para esta pergunta —
    usado pela UI (seletor no sidebar), CLI (--provider) e eval (--provider)."""
    t0 = time.time()
    resolved = llm.resolve(choice)
    out: dict = {"question": question, "engine": engine, "answer": "", "sources": [], "figures": [],
                 "violations": [], "bad_citations": [], "refused": False,
                 "provider": resolved.provider, "model": resolved.model}
    other = _engines_out_of_scope(question)
    if other:
        out.update(refused=True, answer=(
            f"Este manual cobre apenas os motores {', '.join(config.ENGINES)} (Civic 1992-1995). "
            f"O motor {', '.join(other)} não é coberto por este manual: ele pertence a outra geração "
            "(manual de serviço Civic 1996-2000)."))
        out["seconds"] = round(time.time() - t0, 1)
        return out

    with llm.use(choice):
        _fill_answer(question, engine, trans, debug, out, t0)
    out["seconds"] = round(time.time() - t0, 1)
    return out


def _fill_answer(question: str, engine: str | None, trans: str | None, debug: bool,
                 out: dict, t0: float) -> None:
    """Preenche `out` (mutação in-place) com o resultado da pergunta. Separado de `answer()` só para que
    o `with llm.use(choice)` cubra tudo o que chama o LLM (intenção e geração) num único bloco, com um
    ponto só (em `answer()`) para calcular `out["seconds"]` nos vários caminhos de retorno abaixo."""
    info = I.analyze(question)
    intent = info["intent"]
    out["intent"] = intent
    queries = info["queries_en"]
    comp_terms = [info["component_en"], *queries] if info["component_en"] else queries

    sql_rows: list[dict] = []
    extra: list[str] = []
    if intent == "spec" or info["spec_kind"] in ("torque", "clearance", "height", "limit"):
        sql_rows = SQL.specs(comp_terms, engine, info["spec_kind"] if intent == "spec" else "none",
                             side=info["side"])
    if intent == "part_number" or info["codes"]["part_numbers"]:
        for r in SQL.part_numbers(info["codes"]["part_numbers"], [info["component_en"]]):
            extra.append(f"[PART NUMBER] {r['part_number']} — {r['description']} [p. {r['page_label']}]")
    if intent == "special_tool" or info["codes"]["tool_numbers"]:
        for r in SQL.special_tools(info["codes"]["tool_numbers"], [info["component_en"]]):
            extra.append(f"[SPECIAL TOOL] {r['tool_number']} — {r['name']} [p. {r['page_label']}]")
    if intent == "dtc" or info["codes"]["blink"]:
        for r in SQL.dtc(info["codes"]["blink"]):
            extra.append(f"[DIAGNOSTIC CODE] CODE {r['code']} — {r['description']} "
                         f"(procedure: {r['procedure']}, p. {', '.join(r['page_labels'] or [])})")

    types = CHUNK_TYPES.get(intent)
    hits = hybrid.search(queries, question, engine, types=types)
    best_cos = max((h["cos"] for h in hits), default=0.0)
    if not (sql_rows or extra) and (not hits or best_cos < config.MIN_COSINE):
        near = sorted({lab for h in hits[:3] for lab in h["page_labels"] if lab != "?"})
        out.update(refused=True, answer="Não encontrei isso no manual." + (
            f" Páginas mais próximas: {', '.join(near)}." if near else ""))
        return

    context, used = expand.build_context(hits, sql_rows, extra)
    trans_txt = f", transmissão {trans}" if trans else ""
    raw = llm.chat([
        {"role": "system", "content": prompts.SYSTEM},
        {"role": "user", "content": prompts.USER.format(context=context, engine=engine or "não informado",
                                                        trans=trans_txt, question=question)},
    ], temperature=0.0)
    v = V.validate(raw, context, [engine] if engine else [])
    text = v["clean"]
    if v["violations"]:
        text += (f"\n\n⚠️ {len(v['violations'])} linha(s) da resposta foram removidas por conter valores/códigos "
                 "que não constam no trecho recuperado do manual. Confira a página original.")
    labels = sorted({lab for h in used for lab in h["page_labels"] if lab != "?"} |
                    {s["page_label"] for s in sql_rows if s.get("page_label")})
    if re.match(r"(?i)\s*(n[ãa]o consta|n[ãa]o encontr)", text) and len(text) < 220:
        out.update(refused=True, answer="Não encontrei isso no manual." + (
            f" Páginas mais próximas: {', '.join(labels[:4])}." if labels else ""), sources=[])
        return
    if intent in ("procedure", "troubleshooting") and not v["violations"]:
        phrases = [info["component_en"]] + [alt for en in info["glossary"].values() for alt in en]
        steps = expand.matching_steps(sorted({h["procedure_id"] for h in used if h["procedure_id"]}), phrases)
        if steps:
            lines = [f"- Passo {st['n']} [p. {st['label']}]: {st['text']}" + "".join(f" • {n}" for n in st["notes"])
                     for st in steps]
            text += "\n\n**Passos originais do manual (inglês):**\n" + "\n".join(lines)
    cited = list(dict.fromkeys(lab for m in re.finditer(r"\[p\.\s*([^\]]+)\]", text)
                               for lab in re.findall(r"\d{1,2}-\d{1,3}", m.group(1))))
    lead = cited or [s["page_label"] for s in sql_rows[:3] if s.get("page_label")] + \
           [lab for h in used[:2] for lab in h["page_labels"] if lab != "?"]
    out.update(answer=text, raw_answer=raw, violations=v["violations"], bad_citations=v["bad_citations"],
               sources=cited or labels, figures=expand.figures_for(list(dict.fromkeys(lead))))
    if debug:
        out["debug"] = {"info": info, "best_cos": best_cos, "sql_rows": sql_rows, "context": context,
                        "hit_ids": [h["id"] for h in hits[:8]]}
