"""CLI: python -m honda_rag.cli "Qual o torque dos parafusos do cabeçote?" [--engine D16Z6] [--debug]"""
from __future__ import annotations

import argparse
import json
import sys

from honda_rag import config, rag


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("question")
    ap.add_argument("--engine", default=config.DEFAULT_ENGINE)
    ap.add_argument("--trans", choices=["M/T", "A/T"])
    ap.add_argument("--debug", action="store_true")
    a = ap.parse_args()
    r = rag.answer(a.question, a.engine, a.trans, debug=a.debug)
    print(r["answer"])
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
