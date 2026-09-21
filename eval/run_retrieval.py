"""Avaliação só da RECUPERAÇÃO (sem gerar resposta): a página esperada aparece nos top-N chunks?

Serve para comparar modelos de embedding (bge-m3 x Gemini) com as MESMAS consultas: as consultas em
inglês geradas pelo LLM ficam em cache (eval/.intent_cache.json), então trocar EMBED_PROVIDER não muda
a entrada. Também imprime a melhor similaridade de cosseno de cada pergunta, incluindo as que devem ser
recusadas, para calibrar MIN_COSINE.

    python eval/run_retrieval.py                 # usa o cache; gera o que faltar com o LLM configurado
    python eval/run_retrieval.py --refresh       # regera as consultas
    python eval/run_retrieval.py --show          # mostra as páginas dos 3 primeiros chunks
    python eval/run_retrieval.py --file questions_gen.yaml
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from honda_rag import config, rag  # noqa: E402
from honda_rag.retrieval import hybrid, intent as I  # noqa: E402

CACHE = ROOT / "eval" / ".intent_cache.json"


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--show", action="store_true")
    ap.add_argument("--file", default="questions.yaml", help="arquivo de perguntas dentro de eval/")
    a = ap.parse_args()

    qs = yaml.safe_load((ROOT / "eval" / a.file).read_text(encoding="utf-8"))
    cache: dict = {} if a.refresh or not CACHE.exists() else json.loads(CACHE.read_text(encoding="utf-8"))
    print(f"embedding: {config.EMBED_ID} | MIN_COSINE={config.MIN_COSINE}\n")

    rows, cos_ok, cos_refuse = [], [], []
    for q in qs:
        if q["question"] not in cache:
            info = I.analyze(q["question"])
            cache[q["question"]] = {"intent": info["intent"], "queries_en": info["queries_en"]}
            CACHE.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")
        c = cache[q["question"]]
        hits = hybrid.search(c["queries_en"], q["question"], q.get("engine"),
                             types=rag.CHUNK_TYPES.get(c["intent"]))
        best = max((h["cos"] for h in hits), default=0.0)
        if q.get("expect_refusal"):
            cos_refuse.append(best)
            rows.append((q["id"], "recusa", "-", "-", "-", best, ""))
            continue
        cos_ok.append(best)
        exp = set(q.get("expected_pages", []))
        rank = next((i + 1 for i, h in enumerate(hits) if exp & set(h["page_labels"])), None)
        shown = " | ".join(",".join(h["page_labels"]) for h in hits[:3]) if a.show else ""
        rows.append((q["id"], "resposta", rank, rank is not None and rank <= 5,
                     rank is not None and rank <= 10, best, shown))

    print(f"{'id':<24}{'tipo':<10}{'rank':<6}{'@5':<6}{'@10':<6}{'cos':<7}")
    for id_, kind, rank, h5, h10, best, shown in rows:
        print(f"{id_:<24}{kind:<10}{str(rank or '-'):<6}{'sim' if h5 is True else ('-' if h5 == '-' else 'NÃO'):<6}"
              f"{'sim' if h10 is True else ('-' if h10 == '-' else 'NÃO'):<6}{best:<7.3f}{shown}")
    ans = [r for r in rows if r[1] == "resposta"]
    if ans:
        n = len(ans)
        mrr = sum(1 / r[2] for r in ans if r[2]) / n
        print(f"\nhit@5: {sum(r[3] is True for r in ans)}/{n} | hit@10: {sum(r[4] is True for r in ans)}/{n} | MRR: {mrr:.3f}")
    if cos_ok and cos_refuse:
        print(f"cosseno (melhor chunk): respondíveis min {min(cos_ok):.3f} | a recusar máx {max(cos_refuse):.3f}"
              f" -> um limiar entre {max(cos_refuse):.3f} e {min(cos_ok):.3f} separa os dois grupos"
              if max(cos_refuse) < min(cos_ok) else
              f"cosseno: respondíveis min {min(cos_ok):.3f} <= a recusar máx {max(cos_refuse):.3f} (não há limiar limpo)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
