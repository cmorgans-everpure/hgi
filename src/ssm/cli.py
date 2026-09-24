"""CLI: python -m ssm.cli "Company" [--ticker T] [--domain d] [--demo]"""
import argparse, asyncio, json


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("company"); ap.add_argument("--ticker"); ap.add_argument("--domain")
    ap.add_argument("--demo", action="store_true"); ap.add_argument("--include-samples", action="store_true")
    a = ap.parse_args()
    pr = lambda e: print(f"[{e['type']}] {e.get('message', '')}") if e["type"] in ("step", "status", "error") else None
    if a.demo:
        from .demo import run_demo
        r = run_demo(pr)
    else:
        from .agent import run
        r = asyncio.run(run(a.company, a.ticker, a.domain, pr, a.include_samples))
    e = r["model"]["estimates"]
    print(json.dumps({"annual_storage_spend": e["annual_storage_spend"], "range": e["range"],
                      "confidence": e["confidence"], "files": r["files"],
                      "reconciliation": r["reconciliation"]["summary"]}, indent=2, default=str))


if __name__ == "__main__":
    main()
