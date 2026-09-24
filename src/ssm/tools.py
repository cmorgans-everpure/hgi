"""Custom MCP tools exposed to the agents (in-process SDK MCP server 'ssm')."""
from __future__ import annotations
import asyncio, json, os, re
import httpx
from claude_agent_sdk import tool, create_sdk_mcp_server
from . import state as S
from .schema import ALL_FIELDS, NUMERIC_FIELDS, Evidence
from .sources import classify, verify_quote
from .loaders import idc_rows
from . import model as engine

SEC_UA = {"User-Agent": os.getenv("SEC_USER_AGENT", "StorageSpendModeler research@example.com")}


def _ok(obj) -> dict:
    return {"content": [{"type": "text", "text": obj if isinstance(obj, str) else json.dumps(obj, default=str)}]}


def _err(msg) -> dict:
    return {"content": [{"type": "text", "text": f"ERROR: {msg}"}], "is_error": True}


def _num(v):
    if v is None: return None
    if isinstance(v, (int, float)): return float(v)
    s = str(v).lower().replace(",", "").strip()
    m = re.match(r"^\$?\s*(-?[0-9.]+)\s*(k|m|mm|million|b|bn|billion|t|trillion|pb|tb|eb)?", s)
    if not m: return None
    x = float(m.group(1)); u = m.group(2)
    return x * {"k": 1e3, "m": 1e6, "mm": 1e6, "million": 1e6, "b": 1e9, "bn": 1e9, "billion": 1e9,
                "t": 1e12, "trillion": 1e12, "pb": 1000, "eb": 1e6, "tb": 1, None: 1}[u]


# ---------------------------------------------------------------- profile
@tool("set_company_profile", "Set/refresh the resolved company identity and firmographics. Call once identity is confirmed.",
      {"type": "object", "properties": {
          "name": {"type": "string"}, "ticker": {"type": "string"}, "domain": {"type": "string"},
          "industry": {"type": "string", "description": "one of: financial_services, insurance, healthcare, life_sciences, technology, telecom, media, retail, manufacturing, energy_utilities, public_sector, education, transportation, default"},
          "region": {"type": "string", "enum": ["US", "EU", "UK", "APAC"]},
          "revenue_usd": {"type": "number"}, "employees": {"type": "number"}, "fiscal_year": {"type": "string"},
          "notes": {"type": "string"}}, "required": ["name", "industry"]})
async def set_company_profile(args):
    st = S.STATE
    st.profile = st.profile.model_copy(update={k: v for k, v in args.items() if v not in (None, "")})
    st.emit("profile", profile=st.profile.model_dump())
    return _ok({"profile": st.profile.model_dump()})


# ---------------------------------------------------------------- evidence
@tool("record_evidence",
      "Record ONE sourced fact. Must include a VERBATIM quote copied from the source page that supports the value. "
      f"field must be one of: {sorted(ALL_FIELDS)}. Numeric fields need numeric_value in base units "
      "(USD, TB, MWh, count). Percentages/ratios as decimals (25% -> 0.25; DRR 3:1 -> 3). "
      "Use tier_hint for tier-specific capacity/products.",
      {"type": "object", "properties": {
          "field": {"type": "string"}, "value": {"type": "string"}, "numeric_value": {"type": "number"},
          "unit": {"type": "string"},
          "tier_hint": {"type": "string", "enum": ["performance", "general", "capacity", "archive", "cloud"]},
          "source_url": {"type": "string"}, "source_title": {"type": "string"},
          "source_type": {"type": "string", "description": "10-K, 10-Q, 20-F, annual_report, earnings_call, esg_report, company_website, vendor_case_study, vendor_press_release, government_contract, cloud_case_study, press, trade_press, job_posting, conference_talk, blog, other"},
          "quote": {"type": "string"}, "published_date": {"type": "string", "description": "YYYY-MM-DD or YYYY"},
          "found_by": {"type": "string"}},
       "required": ["field", "value", "source_url", "source_type", "quote"]})
async def record_evidence(args):
    st = S.STATE
    f = args["field"]
    if f not in ALL_FIELDS:
        return _err(f"unknown field '{f}'. Allowed: {sorted(ALL_FIELDS)}")
    if len(args.get("quote", "").strip()) < 15:
        return _err("quote must be a verbatim passage (>=15 chars) from the source")
    tier, w, why = classify(args["source_url"], args["source_type"], st.profile.domain)
    if tier == 0:
        return _err(f"source rejected ({why}). Find a primary or trusted source instead.")
    nv = args.get("numeric_value")
    if f in NUMERIC_FIELDS:
        nv = nv if nv is not None else _num(args["value"])
        if nv is None:
            return _err("numeric field requires numeric_value in base units")
    for e in st.evidence:   # de-duplicate
        if e.field == f and e.source_url == args["source_url"] and e.value == args["value"]:
            return _ok({"id": e.id, "duplicate": True})
    ev = Evidence(id=st.next_id(), company=st.profile.name, field=f, value=args["value"], numeric_value=nv,
                  unit=args.get("unit"), tier_hint=args.get("tier_hint"), source_url=args["source_url"],
                  source_type=args["source_type"], source_title=args.get("source_title"), quote=args["quote"],
                  published_date=args.get("published_date"), trust_tier=tier, trust_weight=w,
                  found_by=args.get("found_by"))
    st.evidence.append(ev)
    st.emit("evidence", evidence=ev.model_dump())
    return _ok({"id": ev.id, "trust_tier": tier, "reason": why,
                "note": "tier-4 evidence is weak corroboration only" if tier == 4 else ""})


@tool("verify_evidence", "Fetch each source and confirm its quote appears. Pass ids, or omit to verify all unverified.",
      {"type": "object", "properties": {"ids": {"type": "array", "items": {"type": "string"}}}})
async def verify_evidence(args):
    st = S.STATE
    ids = set(args.get("ids") or [])
    todo = [e for e in st.evidence if (e.id in ids) or (not ids and e.verified is None and not e.verification_note)]
    sem = asyncio.Semaphore(6)
    async def one(e):
        async with sem:
            ok, note = await asyncio.to_thread(verify_quote, e.source_url, e.quote)
            e.verified, e.verification_note = ok, note
            st.emit("verified", id=e.id, ok=ok, note=note)
    await asyncio.gather(*(one(e) for e in todo))
    return _ok({"checked": len(todo), "verified": sum(e.verified is True for e in todo),
                "quote_not_found": [{"id": e.id, "field": e.field, "url": e.source_url, "note": e.verification_note}
                                    for e in todo if e.verified is False],
                "unreachable": [{"id": e.id, "url": e.source_url} for e in todo if e.verified is None]})


@tool("evidence_summary", "Show what has been collected, by field, and which model inputs are still missing.",
      {"type": "object", "properties": {}})
async def evidence_summary(args):
    st = S.STATE
    by = {}
    for e in st.evidence:
        by.setdefault(e.field, []).append(f"{e.id}:{e.value[:60]} (T{e.trust_tier},{'✓' if e.verified else ('✗' if e.verified is False else '?')})")
    key = ["revenue_usd", "employees", "capacity_tb_total", "primary_storage_vendor", "storage_vendor", "storage_model",
           "storage_install_year", "cloud_provider", "data_center_count", "data_center_energy_mwh", "pue", "backup_vendor",
           "wacc_pct", "business_priority", "it_pain_point", "ai_initiative", "incumbent_watts_per_tb",
           "scope2_emissions_tco2e", "capacity_tb_block", "capacity_tb_file"]
    return _ok({"profile": st.profile.model_dump(), "by_field": by,
                "missing_key_fields": [k for k in key if k not in by],
                "unverified_or_failed": [e.id for e in st.evidence if not e.verified]})


# ---------------------------------------------------------------- SEC EDGAR
_TICKERS = None

async def _cik(ticker: str) -> tuple[str, str] | None:
    global _TICKERS
    async with httpx.AsyncClient(headers=SEC_UA, timeout=30) as c:
        if _TICKERS is None:
            _TICKERS = (await c.get("https://www.sec.gov/files/company_tickers.json")).json()
    for v in _TICKERS.values():
        if v["ticker"].upper() == ticker.upper():
            return str(v["cik_str"]).zfill(10), v["title"]
    return None


@tool("sec_company_facts",
      "US-listed companies: pull latest annual revenue, capex and employees from SEC XBRL company facts. "
      "Automatically records them as verified tier-1 evidence.",
      {"type": "object", "properties": {"ticker": {"type": "string"}}, "required": ["ticker"]})
async def sec_company_facts(args):
    st = S.STATE
    hit = await _cik(args["ticker"])
    if not hit: return _err("ticker not found in SEC list (may be non-US; use annual report instead)")
    cik, title = hit
    async with httpx.AsyncClient(headers=SEC_UA, timeout=60) as c:
        r = await c.get(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json")
        if r.status_code != 200: return _err(f"SEC returned {r.status_code}")
        facts = r.json()["facts"]
    def latest(ns, tags, unit):
        best = None
        for t in tags:
            for x in facts.get(ns, {}).get(t, {}).get("units", {}).get(unit, []):
                if x.get("form") in ("10-K", "20-F", "10-K/A") and x.get("fp") == "FY" and (x.get("frame") or "").count("Q") == 0:
                    if best is None or x["end"] > best[1]["end"]: best = (t, x)
        return best
    out = {"cik": cik, "sec_name": title}
    wanted = {"revenue_usd": ("us-gaap", ["Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax", "SalesRevenueNet"], "USD"),
              "capex_usd": ("us-gaap", ["PaymentsToAcquirePropertyPlantAndEquipment", "PaymentsToAcquireProductiveAssets"], "USD"),
              "employees": ("dei", ["EntityNumberOfEmployees"], "pure")}
    for field, (ns, tags, unit) in wanted.items():
        b = latest(ns, tags, unit)
        if not b: continue
        tag, x = b
        url = f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik}&type={x['form']}"
        ev = Evidence(id=st.next_id(), company=st.profile.name, field=field, value=str(x["val"]), numeric_value=float(x["val"]),
                      unit=unit, source_url=url, source_type=x["form"], source_title=f"{title} {x['form']} FY{x.get('fy')} (XBRL {tag})",
                      quote=f"XBRL {ns}:{tag} = {x['val']} for period ending {x['end']} (accession {x.get('accn')})",
                      published_date=x.get("filed"), trust_tier=1, trust_weight=1.0, verified=True,
                      verification_note="structured SEC XBRL API", found_by="sec_company_facts")
        st.evidence.append(ev); st.emit("evidence", evidence=ev.model_dump())
        out[field] = {"value": x["val"], "period_end": x["end"], "form": x["form"], "id": ev.id}
    return _ok(out)


@tool("sec_full_text_search",
      "Search SEC EDGAR full text (filings since 2001) e.g. '\"data center\" storage' or a vendor name, optionally limited to a ticker. "
      "Returns filing links; open them with WebFetch to extract verbatim quotes.",
      {"type": "object", "properties": {"query": {"type": "string"}, "ticker": {"type": "string"},
                                        "forms": {"type": "string", "description": "comma list, default 10-K,10-Q,8-K,20-F"}},
       "required": ["query"]})
async def sec_full_text_search(args):
    params = {"q": args["query"], "forms": args.get("forms") or "10-K,10-Q,8-K,20-F"}
    if args.get("ticker"):
        hit = await _cik(args["ticker"])
        if hit: params["ciks"] = hit[0]
    async with httpx.AsyncClient(headers=SEC_UA, timeout=30) as c:
        r = await c.get("https://efts.sec.gov/LATEST/search-index", params=params)
        if r.status_code != 200: return _err(f"EDGAR FTS returned {r.status_code}")
        hits = r.json().get("hits", {}).get("hits", [])[:15]
    res = []
    for h in hits:
        s_ = h.get("_source", {}); adsh, fn = h.get("_id", ":").split(":", 1)
        cik = (s_.get("ciks") or [""])[0].lstrip("0")
        res.append({"company": (s_.get("display_names") or [""])[0], "form": s_.get("form"), "filed": s_.get("file_date"),
                    "url": f"https://www.sec.gov/Archives/edgar/data/{cik}/{adsh.replace('-', '')}/{fn}"})
    return _ok(res)


# ---------------------------------------------------------------- IDC + model
@tool("idc_price_lookup", "Look up IDC $/TB pricing rows (from the loaded IDC CSV) by vendor and/or model.",
      {"type": "object", "properties": {"vendor": {"type": "string"}, "model": {"type": "string"}}})
async def idc_price_lookup(args):
    rows = idc_rows(args.get("vendor"), args.get("model"), include_samples=S.STATE.include_samples)
    return _ok(rows[:25] or "no matching IDC rows")


@tool("build_model", "Run the deterministic spend model on the evidence collected so far. Returns totals, gaps and weak spots.",
      {"type": "object", "properties": {}})
async def build_model(args):
    st = S.STATE
    m = engine.build(st.profile, st.evidence, include_samples=st.include_samples)
    st.model = m
    st.emit("model", estimates=m["estimates"])
    e = m["estimates"]
    return _ok({"annual_storage_spend": e["annual_storage_spend"], "range": e["range"], "confidence": e["confidence"],
                "methods": e["methods_used"], "capacity": m["capacity"], "breakdown": m["breakdown"],
                "primary_vendor": m["environment"]["primary_vendor"], "provenance": m["provenance"],
                "baseline_5yr_total": m["baseline"]["total"],
                "readiness": [{"input": r["input"], "status": r["status"]} for r in m["readiness"]]})


ALL_TOOLS = [set_company_profile, record_evidence, verify_evidence, evidence_summary,
             sec_company_facts, sec_full_text_search, idc_price_lookup, build_model]
SERVER = create_sdk_mcp_server("ssm", "1.0.0", ALL_TOOLS)
T = lambda *names: [f"mcp__ssm__{n}" for n in names]
