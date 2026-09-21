"""Gera perguntas de avaliação a partir da tabela `specs` — o gabarito sai do próprio banco.

Cada linha de `specs` já tem componente, parâmetro, valor literal e página, que é exatamente o que
`questions.yaml` precisa (`expected_values` + `expected_pages`). Em vez de abrir o PDF e copiar valor a
valor, amostramos o banco e montamos as perguntas — dezenas delas, espalhadas pelo manual inteiro, com
esforço quase zero.

    python eval/gen_questions.py --limit 30                      # imprime na tela
    python eval/gen_questions.py --limit 30 -o eval/questions_gen.yaml
    python eval/gen_questions.py --param torque --engine D16Z6   # recortes
    python eval/run_eval.py --file questions_gen.yaml            # rodar o que saiu daqui

O QUE ISSO MEDE — E O QUE NÃO MEDE
----------------------------------
- Mede recuperação, geração e citação: a resposta traz o valor certo, da página certa?
- **Não** mede a tradução PT->EN. As perguntas saem em português mas com o componente na grafia do
  manual (inglês), de propósito: traduzir pelo `glossary_pt_en` usaria o mesmo glossário que o
  `intent.analyze()` consulta, e a pergunta chegaria mastigada ao sistema. Testar tradução continua
  sendo trabalho das perguntas escritas à mão.
- **Não** é verdade independente: o gabarito veio da mesma extração (OCR) que alimenta a busca. Se o OCR
  leu `0.18` como `0.16`, a pergunta gerada considera `0.16` correto. Uma falha aqui pode ser bug do RAG
  **ou** erro de extração — vale abrir a página original antes de culpar o modelo.

FILTROS (para não gerar pergunta sem resposta única)
----------------------------------------------------
- Só specs com `page_label` (as ~240 páginas sem rótulo não podem ser conferidas).
- Componente+parâmetro+lado+motor com mais de um valor OU mais de uma página: descartado, porque a
  pergunta teria mais de uma resposta certa e o `run_eval.py` exige que TODAS as páginas esperadas
  apareçam nas fontes.
- Páginas marcadas em `review_queue` como `unit_mismatch` ficam de fora (valor provavelmente errado);
  `--include-flagged` traz de volta.

CUIDADO CONHECIDO: o `run_eval.py` compara por substring, e o manual escreve `0.18`. Se o modelo
responder `0,18` (vírgula), a pergunta falha sem ter errado. Se isso aparecer em série, o conserto é no
`norm()` do `run_eval.py`, não aqui.
"""
from __future__ import annotations

import argparse
import json
import random
import re
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from honda_rag import config  # noqa: E402
from honda_rag.db import repo  # noqa: E402

# `parameter` só tem estes três valores (ingest/specs_table.py e ingest/extract.py)
TEMPLATES = {
    "torque": "Qual o torque de {comp}{fast}{side}{eng}?",
    "standard": "Qual o valor padrão de {comp}{side}{eng}?",
    "service_limit": "Qual o limite de serviço de {comp}{side}{eng}?",
}
SIDES = {"IN": " de admissão", "EX": " de escape"}

SQL = """
  SELECT s.component, s.parameter, s.value_text, s.fastener, s.side,
         coalesce(s.applicability->'engine', '[]'::jsonb) AS engines,
         p.page_label, p.pdf_page
    FROM specs s
    JOIN pages p ON p.id = s.page_id
   WHERE p.page_label IS NOT NULL AND p.page_label <> '?'
     AND s.value_text ~ '[0-9]'
     AND length(s.value_text) BETWEEN 3 AND 60
     AND length(s.component) BETWEEN 3 AND 80
     {verified}
     {flagged}
   ORDER BY p.pdf_page, s.id
"""
FLAGGED = ("AND p.pdf_page NOT IN (SELECT (candidates->>'page')::int FROM review_queue "
           "WHERE reason = 'unit_mismatch' AND candidates ? 'page')")


def _norm(s: str) -> str:
    return " ".join(s.split()).lower()


def _slug(s: str) -> str:
    return re.sub(r"_+", "_", re.sub(r"[^a-z0-9]+", "_", _norm(s))).strip("_")[:40]


def fetch(param: str | None, engine: str | None, verified_only: bool, include_flagged: bool) -> list[dict]:
    sql = SQL.format(verified="AND s.verified" if verified_only else "",
                     flagged="" if include_flagged else FLAGGED)
    with repo.connect() as conn:
        conn.row_factory = _dict_row
        rows = conn.execute(sql).fetchall()
    if param:
        rows = [r for r in rows if r["parameter"] == param]
    if engine:
        rows = [r for r in rows if not r["engines"] or engine in r["engines"]]
    return rows


def _dict_row(cursor):
    cols = [c.name for c in cursor.description]
    return lambda values: dict(zip(cols, values))


def group(rows: list[dict]) -> list[dict]:
    """Junta specs equivalentes e descarta as ambíguas (mais de um valor ou mais de uma página)."""
    buckets: dict[tuple, list[dict]] = {}
    for r in rows:
        engines = sorted(r["engines"]) if r["engines"] else []
        key = (_norm(r["component"]), r["parameter"], r["side"] or "", tuple(engines), r["fastener"] or "")
        buckets.setdefault(key, []).append(r)
    out = []
    for key, rs in buckets.items():
        if len({_norm(r["value_text"]) for r in rs}) > 1:
            continue                                    # mesma pergunta, respostas diferentes
        if len({r["page_label"] for r in rs}) > 1:
            continue                                    # run_eval exige TODAS as páginas nas fontes
        out.append({**rs[0], "engines": list(key[3])})
    return out


def spread(items: list[dict], limit: int, seed: int) -> list[dict]:
    """Amostra espalhada pelo manual: sorteia dentro de cada página e percorre as páginas em rodízio,
    para não sair 30 perguntas da mesma tabela da seção 3."""
    rnd = random.Random(seed)
    by_page: dict[str, list[dict]] = {}
    for it in items:
        by_page.setdefault(it["page_label"], []).append(it)
    for v in by_page.values():
        rnd.shuffle(v)
    pages = sorted(by_page, key=lambda p: by_page[p][0]["pdf_page"])
    rnd.shuffle(pages)
    out: list[dict] = []
    while len(out) < limit and any(by_page[p] for p in pages):
        for p in pages:
            if by_page[p]:
                out.append(by_page[p].pop())
            if len(out) >= limit:
                break
    return out


def to_question(it: dict, used_ids: set[str]) -> dict:
    eng = it["engines"]
    # cita o motor só quando a spec é específica: se vale para todos, mencionar seria ruído
    engine = eng[0] if eng else config.DEFAULT_ENGINE
    fast = f" (parafuso {it['fastener']})" if it["parameter"] == "torque" and it["fastener"] else ""
    q = TEMPLATES[it["parameter"]].format(
        comp=it["component"].rstrip(" .:"), fast=fast,
        side=SIDES.get(it["side"] or "", ""), eng=f" do {engine}" if eng else "")
    base = f"gen_{it['parameter'][:4]}_{_slug(it['component'])}"
    qid, n = base, 2
    while qid in used_ids:
        qid, n = f"{base}_{n}", n + 1
    used_ids.add(qid)
    return {"id": qid, "question": q, "engine": engine, "intent": "spec",
            "expected_pages": [it["page_label"]], "expected_values": [it["value_text"]]}


def dump(qs: list[dict], argv: list[str], total: int) -> str:
    head = [
        "# GERADO por eval/gen_questions.py — não edite à mão (regenere ou promova para questions.yaml).",
        f"# {date.today().isoformat()} · {' '.join(argv)} · {len(qs)} de {total} specs elegíveis",
        "# O gabarito vem da tabela `specs`, extraída por OCR: uma falha pode ser do RAG ou da extração.",
        "# Perguntas em PT com o componente na grafia do manual: NÃO testam a tradução PT->EN.",
        "",
    ]
    for q in qs:
        head += [
            f"- id: {q['id']}",
            f"  question: {json.dumps(q['question'], ensure_ascii=False)}",
            f"  engine: {q['engine']}",
            f"  intent: {q['intent']}",
            f"  expected_pages: [{json.dumps(q['expected_pages'][0], ensure_ascii=False)}]",
            f"  expected_values: [{json.dumps(q['expected_values'][0], ensure_ascii=False)}]",
        ]
    return "\n".join(head) + "\n"


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--limit", type=int, default=30)
    ap.add_argument("--param", choices=sorted(TEMPLATES))
    ap.add_argument("--engine", choices=config.ENGINES)
    ap.add_argument("--seed", type=int, default=0, help="mesma semente = mesma amostra")
    ap.add_argument("--verified-only", action="store_true",
                    help="só specs conferidas na revisão (a ingestão grava verified=FALSE: pode vir vazio)")
    ap.add_argument("--include-flagged", action="store_true",
                    help="inclui páginas marcadas como unit_mismatch na review_queue")
    ap.add_argument("-o", "--out", type=Path)
    a = ap.parse_args()

    rows = fetch(a.param, a.engine, a.verified_only, a.include_flagged)
    items = group(rows)
    picked = spread(items, a.limit, a.seed)
    if not picked:
        print("Nenhuma spec elegível. Sem --verified-only o banco costuma ter material; confira se a "
              "ingestão rodou (--stage load) e se as páginas têm page_label.", file=sys.stderr)
        return 1

    used: set[str] = set()
    text = dump([to_question(it, used) for it in picked], sys.argv[1:], len(items))
    if a.out:
        a.out.write_text(text, encoding="utf-8")
        print(f"{len(picked)} perguntas em {a.out} (de {len(items)} specs elegíveis, {len(rows)} linhas brutas)")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
