"""Load HGI and IDC CSV exports using configurable column maps."""
from __future__ import annotations
import csv, re
from pathlib import Path
from .config import column_maps
from . import DATA


def _num(v) -> float | None:
    if v is None: return None
    s = re.sub(r"[^0-9.\-]", "", str(v))
    try: return float(s) if s not in ("", "-", ".") else None
    except ValueError: return None


def _canon_rows(path: Path, cmap: dict) -> list[dict]:
    with open(path, newline="", encoding="utf-8-sig") as f:
        rdr = csv.DictReader(f)
        headers = {h.strip().lower(): h for h in (rdr.fieldnames or [])}
        pick = {k: next((headers[a.lower()] for a in alts if a.lower() in headers), None)
                for k, alts in cmap.items()}
        return [{k: (row.get(src) or "").strip() if src else "" for k, src in pick.items()} for row in rdr]


def _files(sub: str) -> list[Path]:
    return sorted((DATA / sub).glob("*.csv"))


def norm_company(s: str) -> str:
    s = s.lower()
    s = re.sub(r"\b(inc|incorporated|corp|corporation|co|company|plc|ltd|limited|llc|ag|sa|nv|group|holdings?)\b\.?", "", s)
    return re.sub(r"[^a-z0-9]", "", s)


def hgi_rows(company: str, domain: str | None = None, include_samples: bool = False) -> list[dict]:
    cm = column_maps()
    want, dom = norm_company(company), (domain or "").lower().removeprefix("www.")
    out = []
    for p in _files("hgi"):
        if p.name.startswith("SAMPLE") and not include_samples: continue
        for r in _canon_rows(p, cm["hgi"]):
            if norm_company(r["company"]) == want or (dom and r["domain"].lower().removeprefix("www.") == dom):
                r["spend_usd"] = _num(r["spend_usd"]); r["_file"] = p.name
                r["is_storage"] = any(k in r["category"].lower() for k in cm["hgi_storage_categories"])
                out.append(r)
    return out


def idc_tier(row: dict) -> str | None:
    txt = f'{row.get("segment","")} {row.get("media","")} {row.get("model","")}'.lower()
    for rule in column_maps()["idc_tier_rules"]:
        if any(k in txt for k in rule["contains"]): return rule["tier"]
    return None


def idc_rows(vendor: str | None = None, model: str | None = None, include_samples: bool = False) -> list[dict]:
    cm = column_maps()
    out = []
    for p in _files("idc"):
        if p.name.startswith("SAMPLE") and not include_samples: continue
        for r in _canon_rows(p, cm["idc"]):
            r["usd_per_tb"] = _num(r["usd_per_tb"]) or _num(r.get("usd_per_tb_raw"))
            r["tier"] = idc_tier(r); r["_file"] = p.name
            if vendor and norm_company(vendor) not in norm_company(r["vendor"]) and norm_company(r["vendor"]) not in norm_company(vendor):
                continue
            if model and model.lower() not in r["model"].lower() and r["model"].lower() not in model.lower():
                continue
            out.append(r)
    return out


def idc_tier_prices(vendors: list[str], include_samples: bool = False) -> dict[str, dict]:
    """Median IDC $/TB per tier, preferring rows for the company's identified vendors."""
    from statistics import median
    res = {}
    all_rows = idc_rows(include_samples=include_samples)
    vn = [norm_company(v) for v in vendors if v]
    for tier in ("performance", "general", "capacity", "archive"):
        rows = [r for r in all_rows if r["tier"] == tier and r["usd_per_tb"]]
        vend = [r for r in rows if any(v and (v in norm_company(r["vendor"]) or norm_company(r["vendor"]) in v) for v in vn)]
        use = vend or rows
        if use:
            res[tier] = {"usd_per_tb": median(r["usd_per_tb"] for r in use),
                         "basis": ("IDC vendor-matched: " if vend else "IDC tier median: ")
                                  + ", ".join(sorted({f'{r["vendor"]} {r["model"]}' for r in use}))[:200],
                         "n": len(use)}
    return res
