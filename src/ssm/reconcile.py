"""Compare our independent model against HGI spend data and IDC pricing."""
from __future__ import annotations
from collections import defaultdict
from .config import assumptions
from .loaders import hgi_rows, idc_rows, norm_company


def _var(ours, ref):
    if ours is None or not ref: return None
    return (ours - ref) / ref


def _status(v, thr):
    if v is None: return "n/a"
    return "ALIGNED" if abs(v) <= thr else ("OURS HIGHER" if v > 0 else "OURS LOWER")


def reconcile(model: dict, include_samples: bool = False) -> dict:
    thr = assumptions()["reconciliation"]["variance_flag_pct"]
    co = model["company"]
    rows = hgi_rows(co["name"], co.get("domain"), include_samples=include_samples)
    st = [r for r in rows if r["is_storage"]]
    checks = []

    # 1. total spend
    hgi_total = sum(r["spend_usd"] or 0 for r in st) or None
    ours = model["estimates"]["annual_storage_spend"]
    v = _var(ours, hgi_total)
    checks.append({"check": "Total annual storage spend", "ours": ours, "reference": hgi_total, "source": "HGI",
                   "variance_pct": v, "status": _status(v, thr),
                   "note": "HGI sums storage-category rows only" if hgi_total else "No HGI rows for this company"})

    # 2. cloud storage
    hgi_cloud = sum(r["spend_usd"] or 0 for r in st if "cloud" in r["category"].lower()) or None
    v = _var(model["breakdown"]["cloud_storage"], hgi_cloud)
    checks.append({"check": "Cloud storage spend", "ours": model["breakdown"]["cloud_storage"], "reference": hgi_cloud,
                   "source": "HGI", "variance_pct": v, "status": _status(v, thr), "note": ""})

    # 3. vendor footprint
    hv = defaultdict(float)
    for r in st: hv[r["vendor"]] += r["spend_usd"] or 0
    hgi_vendors = sorted(hv, key=hv.get, reverse=True)
    ours_v = model["environment"]["vendors_ranked"]
    on = {norm_company(x) for x in ours_v}; hn = {norm_company(x) for x in hgi_vendors}
    match = lambda a, S: any(a in b or b in a for b in S)
    confirmed = [x for x in ours_v if match(norm_company(x), hn)]
    only_ours = [x for x in ours_v if not match(norm_company(x), hn)]
    only_hgi = [x for x in hgi_vendors if not match(norm_company(x), on)]
    pv, hp = model["environment"]["primary_vendor"], (hgi_vendors[0] if hgi_vendors else None)
    checks.append({"check": "Primary storage vendor", "ours": pv, "reference": hp, "source": "HGI", "variance_pct": None,
                   "status": "n/a" if not (pv and hp) else ("ALIGNED" if match(norm_company(pv), {norm_company(hp)}) else "MISMATCH"),
                   "note": ""})

    # 4. IDC price sanity per tier
    for t, p in model["pricing"]["usd_per_tb"].items():
        basis = model["pricing"]["basis"][t]
        ref_rows = [r for r in idc_rows(include_samples=include_samples) if r["tier"] == t and r["usd_per_tb"]]
        ref = sorted(r["usd_per_tb"] for r in ref_rows)[len(ref_rows) // 2] if ref_rows else None
        v = _var(p, ref)
        checks.append({"check": f"$/TB {t}", "ours": p, "reference": ref, "source": "IDC tier median",
                       "variance_pct": v, "status": _status(v, thr), "note": basis})

    # 5. HGI line items with implied capacity (HGI $ / IDC $/TB x refresh) for matched products
    implied = []
    for r in st:
        m = idc_rows(r["vendor"], r["product"], include_samples=include_samples)
        if m and r["spend_usd"] and m[0]["usd_per_tb"]:
            implied.append({"vendor": r["vendor"], "product": r["product"], "hgi_spend": r["spend_usd"],
                            "idc_usd_per_tb": m[0]["usd_per_tb"],
                            "implied_tb_per_year": r["spend_usd"] / m[0]["usd_per_tb"]})
    flags = [c for c in checks if c["status"] in ("OURS HIGHER", "OURS LOWER", "MISMATCH")]
    return {"threshold": thr, "checks": checks, "hgi_rows": rows, "hgi_storage_total": hgi_total,
            "vendors": {"confirmed_by_hgi": confirmed, "found_only_by_research": only_ours, "only_in_hgi": only_hgi},
            "hgi_implied_capacity": implied, "flag_count": len(flags),
            "summary": (f"{len(flags)} of {sum(c['status']!='n/a' for c in checks)} comparable checks exceed ±{thr:.0%}"
                        if checks else "nothing to compare")}
