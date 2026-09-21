"""Avaliação: recuperação (página esperada nas fontes), exatidão numérica, recusa e violações do validador.

    python eval/run_eval.py [--only id1,id2] [--load] [--file questions_gen.yaml]
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from honda_rag import rag  # noqa: E402
from honda_rag.db import repo  # noqa: E402


def norm(s: str) -> str:
    return " ".join(s.replace("–", "-").replace("—", "-").split()).lower()


def load_questions(qs: list[dict]) -> None:
    with repo.connect() as conn:
        conn.execute("DELETE FROM eval_questions")
        for q in qs:
            conn.execute(
                "INSERT INTO eval_questions (question_pt,intent,engine,expected_pages,expected_values) "
                "VALUES (%s,%s,%s,%s,%s)",
                (q["question"], q.get("intent"), q.get("engine"), q.get("expected_pages", []),
                 q.get("expected_values", [])))
        conn.commit()


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("--only")
    ap.add_argument("--file", default="questions.yaml", help="arquivo de perguntas dentro de eval/")
    ap.add_argument("--load", action="store_true", help="grava as perguntas em eval_questions")
    a = ap.parse_args()
    qs = yaml.safe_load((ROOT / "eval" / a.file).read_text(encoding="utf-8"))
    if a.load:
        load_questions(qs)
    if a.only:
        wanted = set(a.only.split(","))
        qs = [q for q in qs if q["id"] in wanted]

    rows, failures = [], 0
    for q in qs:
        t0 = time.time()
        r = rag.answer(q["question"], q.get("engine"))
        ans = norm(r["answer"])
        if q.get("expect_refusal"):
            ok = r["refused"]
            rows.append((q["id"], "recusa", ok, "-", "-", len(r["violations"]), time.time() - t0))
        else:
            page_hit = all(p in r["sources"] for p in q.get("expected_pages", []))
            vals = [norm(v) in ans for v in q.get("expected_values", [])]
            txt = [norm(t) in ans for t in q.get("expected_text", [])]
            ok = page_hit and all(vals) and all(txt) and not r["refused"]
            rows.append((q["id"], "resposta", ok, page_hit, f"{sum(vals)}/{len(vals)}", len(r["violations"]),
                         time.time() - t0))
        if not ok:
            failures += 1
            print(f"\n[FALHA] {q['id']}: {q['question']}\n{r['answer'][:700]}\nfontes: {r['sources']}")
    print(f"\n{'id':<18}{'tipo':<10}{'ok':<6}{'pág.':<7}{'valores':<9}{'viol.':<7}{'s':<5}")
    for id_, kind, ok, ph, vals, viol, sec in rows:
        print(f"{id_:<18}{kind:<10}{'sim' if ok else 'NÃO':<6}{str(ph):<7}{vals:<9}{viol:<7}{sec:<5.0f}")
    print(f"\n{len(rows) - failures}/{len(rows)} corretas; violações do validador: {sum(r[5] for r in rows)}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
