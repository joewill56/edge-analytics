"""CLI: python manage.py init | ingest [CODE] | train [CODE]"""
import sys

import model
import pipeline
from config import LEAGUES


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "init"
    codes = [sys.argv[2]] if len(sys.argv) > 2 else [m["code"] for m in LEAGUES.values()]
    for code in codes:
        if cmd in ("init", "ingest"):
            print(f"[{code}] ingested {pipeline.ingest_league(code)} matches")
        if cmd in ("init", "train"):
            try:
                print(f"[{code}] metrics:", model.train_league(code))
            except Exception as exc:
                print(f"[{code}] training failed: {exc}")


if __name__ == "__main__":
    main()
