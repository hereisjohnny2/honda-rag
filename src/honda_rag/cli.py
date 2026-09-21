"""CLI: python -m honda_rag.cli "Qual o torque dos parafusos do cabeçote?" [--engine D16Z6]
                                [--provider gemini] [--model gemini-2.5-pro] [--debug]"""
from __future__ import annotations

import argparse
import json
import sys

from honda_rag import config, llm, providers, rag


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("question")
    ap.add_argument("--engine", default=config.DEFAULT_ENGINE)
    ap.add_argument("--trans", choices=["M/T", "A/T"])
    ap.add_argument("--provider", choices=sorted(providers.PROVIDERS),
                    help="sobrepõe LLM_PROVIDER do .env só para esta pergunta")
    ap.add_argument("--model", help="sobrepõe o modelo padrão do provedor (com --provider ou sem)")
    ap.add_argument("--debug", action="store_true")
    a = ap.parse_args()
    choice = llm.Choice(a.provider, a.model) if a.provider else (
        llm.Choice(config.LLM_PROVIDER, a.model) if a.model else None)
    r = rag.answer(a.question, a.engine, a.trans, debug=a.debug, choice=choice)
    print(r["answer"])
    if a.debug or a.provider or a.model:
        print(f"\n[{r['provider']} · {r['model']} · {r['seconds']}s]")
    if r["sources"]:
        print("\nFontes: " + ", ".join("p. " + s for s in r["sources"]))
    if r["figures"]:
        print("Figuras: " + ", ".join(f["image_path"] for f in r["figures"][:4]))
    if a.debug:
        print("\n--- DEBUG ---")
        print(json.dumps(r.get("debug"), ensure_ascii=False, indent=1, default=str)[:6000])
        if r["violations"]:
            print("violações:", r["violations"])


if __name__ == "__main__":
    main()
