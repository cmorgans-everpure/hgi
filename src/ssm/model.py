"""Storage spend estimation engine (deterministic, auditable).

Three independent views are triangulated:
  1. Top-down      revenue x IT% x storage%      (or disclosed IT / storage spend)
  2. Bottom-up     capacity by tier x $/TB (IDC) + support + power & cooling + cloud + admin
  3. Energy check  ESG data-centre MWh -> storage kWh -> implied capacity
HGI is deliberately NOT an input: it is what we verify against (see reconcile.py).
"""
from __future__ import annotations
from collections import defaultdict
from .config import assumptions
from .loaders import idc_tier_prices
from .schema import CompanyProfile, Evidence

ONPREM = ("performance", "general", "capacity", "archive")
TIERS = ONPREM + ("cloud",)


def _score(e: Evidence) -> float:
    v = {True: 1.2, None: 1.0, False: 0.4}[e.verified]
    rec = 1.0
    if e.published_date and e.published_date[:4].isdigit():
        age = max(0, assumptions()["model_year"] - int(e.published_date[:4]))
        rec = max(0.4, 1 - 0.12 * age)
    return e.trust_weight * v * rec


def best(evidence: list[Evidence], field: str) -> Evidence | None:
    c = [e for e in evidence if e.field == field and (e.numeric_value is not None or field not in _numeric())]
    c = [e for e in c if e.trust_tier > 0]
    return max(c, key=_score) if c else None


def _numeric():
    from .schema import NUMERIC_FIELDS
    return NUMERIC_FIELDS


def build(profile: CompanyProfile, evidence: list[Evidence], include_samples: bool = False) -> dict:
    A = assumptions()
    ind = profile.industry if profile.industry in A["it_pct_revenue"] else "default"
    used: dict[str, str] = {}          # input -> provenance string
    def pick(field, fallback, label):
        e = best(evidence, field)
        if e is not None and e.numeric_value is not None:
            used[label] = f"evidence {e.id} (tier {e.trust_tier}{', verified' if e.verified else ''})"
            return e.numeric_value
        if fallback is not None:
            used[label] = "company profile / SEC" if label in ("revenue", "employees") else "assumption default"
        return fallback

    revenue = pick("revenue_usd", profile.revenue_usd, "revenue")
    employees = pick("employees", profile.employees, "employees")
    pue = pick("pue", A["pue"], "pue")
    kwh = pick("usd_per_kwh", A["region_usd_per_kwh"].get(profile.region, A["usd_per_kwh"]), "usd_per_kwh")

    # ---------------- environment / vendors ----------------
    vend_score = defaultdict(float)
    for e in evidence:
        if e.field in ("primary_storage_vendor", "storage_vendor", "storage_product", "storage_model") and e.trust_tier > 0:
            name = e.value.split("|")[0].strip() if e.field != "storage_vendor" else e.value.strip()
            if e.field in ("primary_storage_vendor", "storage_vendor"):
                vend_score[name] += _score(e) * (2 if e.field == "primary_storage_vendor" else 1)
    vendors = sorted(vend_score, key=vend_score.get, reverse=True)
    pv = best(evidence, "primary_storage_vendor")
    primary_vendor = pv.value if pv else (vendors[0] if vendors else None)

    # ---------------- capacity ----------------
    wpt = dict(A["watts_per_tb"])
    for v in vendors[:1]:
        wpt.update(A.get("vendor_watts_per_tb", {}).get(v, {}))
    inc_w = best(evidence, "incumbent_watts_per_tb")
    if inc_w:
        for t in ([inc_w.tier_hint] if inc_w.tier_hint in ONPREM else ["performance", "general"]):
            wpt[t] = inc_w.numeric_value
        used["watts_per_tb"] = f"evidence {inc_w.id} (incumbent datasheet)"
    mix = dict(A["default_tier_mix"])
    tier_disc = {t: best(evidence, f"capacity_tb_{t}") for t in TIERS}
    tier_disc = {t: e.numeric_value for t, e in tier_disc.items() if e}

    cap_methods = {}
    tot = best(evidence, "capacity_tb_total")
    if tot:
        cap_methods["disclosed"] = tot.numeric_value; used["capacity_total"] = f"evidence {tot.id}"
    elif tier_disc and sum(mix[t] for t in tier_disc) > 0.5:
        cap_methods["disclosed"] = sum(tier_disc.values()) / sum(mix[t] for t in tier_disc)
        used["capacity_total"] = "scaled from disclosed tier capacities"
    if employees:
        cap_methods["heuristic"] = employees * A["tb_per_employee"].get(ind, A["tb_per_employee"]["default"])
    mwh = best(evidence, "data_center_energy_mwh")
    if mwh:
        onprem_share = 1 - mix["cloud"]
        blended_w = sum(mix[t] * wpt[t] for t in ONPREM) / onprem_share
        storage_kwh = mwh.numeric_value * 1000 / pue * A["storage_share_of_it_energy"]
        cap_methods["energy_derived"] = storage_kwh / (blended_w * 8.76) / onprem_share
    if "disclosed" in cap_methods:
        total_tb, cap_basis = cap_methods["disclosed"], "disclosed"
    elif cap_methods:
        w = {"heuristic": A["method_weights"]["bottom_up_heuristic"], "energy_derived": A["method_weights"]["energy_derived"]}
        total_tb = sum(cap_methods[k] * w[k] for k in cap_methods) / sum(w[k] for k in cap_methods)
        cap_basis = "+".join(cap_methods)
        used.setdefault("capacity_total", cap_basis)
    else:
        total_tb, cap_basis = 0.0, "none"

    # tier split: disclosed tiers fixed, remainder shared by default mix
    tb = {}
    if tier_disc and total_tb:
        rem = max(total_tb - sum(tier_disc.values()), 0)
        free = [t for t in TIERS if t not in tier_disc]
        fs = sum(mix[t] for t in free) or 1
        for t in TIERS:
            tb[t] = tier_disc.get(t, rem * mix[t] / fs)
    else:
        tb = {t: total_tb * mix[t] for t in TIERS}
    total_tb = sum(tb.values())
    mix_used = {t: (tb[t] / total_tb if total_tb else mix[t]) for t in TIERS}

    # ---------------- prices ----------------
    idc = idc_tier_prices(vendors, include_samples=include_samples)
    price, price_basis = {}, {}
    for t in ONPREM:
        if t in idc:
            price[t], price_basis[t] = idc[t]["usd_per_tb"], idc[t]["basis"]
        else:
            price[t], price_basis[t] = A["usd_per_tb"][t], "assumption default"

    # ---------------- bottom-up spend ----------------
    lines = {}
    for t in ONPREM:
        acq = tb[t] * price[t]
        kwh_y = tb[t] * wpt[t] * 8.76
        p_usd = kwh_y * kwh
        lines[t] = {"tb": tb[t], "usd_per_tb": price[t], "acquisition": acq,
                    "hardware_annual": acq / A["refresh_years"], "support_annual": acq * A["support_pct_of_acquisition"],
                    "watts_per_tb": wpt[t], "kwh_year": kwh_y, "power_usd": p_usd, "cooling_usd": p_usd * (pue - 1)}
    cloud_disc = best(evidence, "cloud_storage_spend_usd")
    cloud_usd = cloud_disc.numeric_value if cloud_disc else tb["cloud"] * 1024 * A["cloud_usd_per_gb_month"] * 12
    onprem_tb = sum(tb[t] for t in ONPREM)
    admin = onprem_tb / A["tb_per_admin_fte"] * A["admin_fte_loaded_cost"]
    # ---------------- data-centre space ----------------
    tpr = dict(A["tb_per_ru"])
    inc_d = best(evidence, "incumbent_tb_per_ru")
    if inc_d:
        for t in ([inc_d.tier_hint] if inc_d.tier_hint in ONPREM else ["performance", "general"]):
            tpr[t] = inc_d.numeric_value
    ov = A["rack_overhead_ru_per_rack"]
    for t in ONPREM:
        lines[t]["tb_per_ru"] = tpr[t]
        lines[t]["rack_units"] = tb[t] / tpr[t] * 42 / (42 - ov) if tpr[t] else 0
    ru_e = best(evidence, "storage_rack_units")
    total_ru = ru_e.numeric_value if ru_e else sum(lines[t]["rack_units"] for t in ONPREM)
    colo = pick("colocation_usd_per_ru_month", A["colocation_usd_per_ru_month"].get(profile.region, 45), "usd_per_ru_month")
    space_usd = total_ru * colo * 12

    # ---------------- networking ----------------
    dc = best(evidence, "data_center_count")
    sites = max(1, int(dc.numeric_value)) if dc else 1
    sw_e = best(evidence, "network_switch_count")
    sp_, pr_ = A["switches_per_site"], A["switch_price_usd"]
    per_site_capex = sp_["data"] * pr_["data"] + sp_["management"] * pr_["management"]
    n_switch = sw_e.numeric_value if sw_e else sites * (sp_["data"] + sp_["management"])
    avg_sw = per_site_capex / (sp_["data"] + sp_["management"])
    net_capex = n_switch * avg_sw
    net_usd = net_capex / A["refresh_years"] + net_capex * A["switch_support_pct"]

    cat = {
        "hardware_annualized": sum(l["hardware_annual"] for l in lines.values()),
        "maintenance_support": sum(l["support_annual"] for l in lines.values()),
        "power": sum(l["power_usd"] for l in lines.values()),
        "cooling_facility": sum(l["cooling_usd"] for l in lines.values()),
        "cloud_storage": cloud_usd,
        "admin_labor": admin,
        "data_center_space": space_usd,
        "networking": net_usd,
    }
    bottom_up = sum(cat.values())

    # ---------------- top-down ----------------
    sp = A["storage_pct_it_spend"]
    it_e = best(evidence, "it_spend_usd")
    it_spend = it_e.numeric_value if it_e else (revenue * A["it_pct_revenue"][ind] if revenue else None)
    top_down = {k: it_spend * sp[k] for k in ("low", "mid", "high")} if it_spend else None
    disc_spend = best(evidence, "storage_spend_usd")

    # ---------------- triangulate ----------------
    mw = A["method_weights"]
    methods = {}
    if top_down: methods["top_down"] = (top_down["mid"], mw["top_down"])
    if total_tb:
        methods["bottom_up"] = (bottom_up, mw["bottom_up_disclosed"] if cap_basis == "disclosed" else mw["bottom_up_heuristic"])
    if disc_spend: methods["disclosed_spend"] = (disc_spend.numeric_value, 0.9 * disc_spend.trust_weight)
    est = sum(v * w for v, w in methods.values()) / sum(w for _, w in methods.values()) if methods else None
    vals = [v for v, _ in methods.values()] + ([top_down["low"], top_down["high"]] if top_down else [])
    if est:
        u = 0.20 if cap_basis == "disclosed" or disc_spend else 0.40      # minimum uncertainty band
        rng = (min(vals + [est * (1 - u)]), max(vals + [est * (1 + u)]))
    else:
        rng = (None, None)

    # confidence: evidence quality + method agreement
    strong = [e for e in evidence if e.trust_tier in (1, 2) and e.verified]
    q = min(1.0, len(strong) / 8)
    agree = 1 - min(1.0, (rng[1] - rng[0]) / est) if est and len(methods) > 1 else 0.3
    confidence = round(100 * (0.25 + 0.45 * q + 0.30 * agree) if methods else 0)

    # ---------------- capacity profile (incumbent) ----------------
    pm = dict(A["protocol_mix"])
    pdisc = {p: best(evidence, f"capacity_tb_{p}") for p in pm}
    pdisc = {p: e.numeric_value for p, e in pdisc.items() if e}
    if pdisc:
        rem = max(onprem_tb - sum(pdisc.values()), 0); free = [p for p in pm if p not in pdisc]
        fs = sum(pm[p] for p in free) or 1
        prot_tb = {p: pdisc.get(p, rem * pm[p] / fs) for p in pm}
    else:
        prot_tb = {p: onprem_tb * pm[p] for p in pm}
    drr_e = best(evidence, "data_reduction_ratio") or best(evidence, "incumbent_drr")
    usable_pct = pick("usable_pct", A["usable_pct"], "usable_pct")
    util = pick("utilization_pct", A["utilization_pct"], "utilization_pct")
    growth = pick("data_growth_pct", A["data_growth_pct"], "data_growth_pct")
    profile_rows = {}
    for p_, u in prot_tb.items():
        drr = drr_e.numeric_value if drr_e else A["incumbent_drr"][p_]
        profile_rows[p_] = {"usable_tb": u, "raw_tb": u / usable_pct if usable_pct else None, "drr": drr,
                            "stored_effective_tb": u * util * drr}
    used["data_reduction_ratio"] = f"evidence {drr_e.id}" if drr_e else "assumption default"

    # ---------------- 5-year current-state baseline ----------------
    yrs = A["baseline_years"]
    growth_driven = {"hardware_annualized", "maintenance_support", "power", "cooling_facility", "cloud_storage",
                     "admin_labor", "data_center_space"}
    groups = {"Platform (HW, SW & support)": ["hardware_annualized", "maintenance_support"],
              "Networking": ["networking"], "Power & Cooling": ["power", "cooling_facility"],
              "Data Centre Space": ["data_center_space"], "Admin labor": ["admin_labor"], "Cloud storage": ["cloud_storage"]}
    by_year = {}
    for g, ks in groups.items():
        by_year[g] = [sum(cat[k] * ((1 + growth) ** y if k in growth_driven else 1) for k in ks) for y in range(yrs)]
    baseline = {"years": yrs, "growth_pct": growth, "rows": by_year,
                "totals_by_year": [sum(v[y] for v in by_year.values()) for y in range(yrs)]}
    baseline["total"] = sum(baseline["totals_by_year"])

    wacc = pick("wacc_pct", A["wacc_pct"].get(ind, A["wacc_pct"]["default"]), "wacc")
    baseline["npv"] = sum(v / (1 + wacc) ** (y + 1) for y, v in enumerate(baseline["totals_by_year"]))
    fac_kwh = sum(l["kwh_year"] for l in lines.values()) * pue
    grid = pick("grid_kgco2e_per_kwh", A["grid_kgco2e_per_kwh"].get(profile.region, 0.37), "grid_factor")
    s2 = best(evidence, "scope2_emissions_tco2e")

    # ---------------- refresh timing ----------------
    refresh = []
    for e in evidence:
        if e.field == "storage_install_year" and e.numeric_value and e.trust_tier > 0:
            y = int(e.numeric_value); due = y + A["refresh_years"]
            refresh.append({"system": e.value, "install_year": y, "age_years": A["model_year"] - y, "refresh_due": due,
                            "status": "OVERDUE" if due < A["model_year"] else ("DUE ≤12M" if due <= A["model_year"] + 1 else "later"),
                            "evidence_id": e.id})
    for e in evidence:
        if e.field in ("storage_refresh_plan", "support_contract_end") and e.trust_tier > 0:
            refresh.append({"system": e.value, "install_year": None, "age_years": None,
                            "refresh_due": e.numeric_value or e.published_date, "status": e.field.replace("_", " "),
                            "evidence_id": e.id})

    narrative = {k: [{"value": e.value, "source": e.source_url, "id": e.id, "trust_tier": e.trust_tier}
                     for e in sorted([x for x in evidence if x.field == k and x.trust_tier > 0], key=_score, reverse=True)]
                 for k in ("business_priority", "ai_initiative", "it_pain_point", "storage_initiative", "sustainability_target")}

    return {
        "company": profile.model_dump(), "industry_used": ind,
        "capacity_profile": {"by_protocol": profile_rows, "usable_pct": usable_pct, "utilization_pct": util,
                             "onprem_usable_tb": onprem_tb},
        "space": {"rack_units": total_ru, "usd_per_ru_month": colo, "annual_usd": space_usd,
                  "basis": f"evidence {ru_e.id}" if ru_e else "capacity / TB-per-RU density"},
        "networking": {"sites": sites, "switches": n_switch, "capex": net_capex, "annual_usd": net_usd},
        "baseline": baseline,
        "finance_esg": {"wacc_pct": wacc, "grid_kgco2e_per_kwh": grid, "storage_facility_kwh_year": fac_kwh,
                        "storage_tco2e_year": fac_kwh * grid / 1000,
                        "company_scope2_tco2e": s2.numeric_value if s2 else None},
        "refresh": refresh,
        "narrative": narrative,
        "inputs": {"revenue_usd": revenue, "employees": employees, "pue": pue, "usd_per_kwh": kwh,
                   "it_spend_usd": it_spend, "refresh_years": A["refresh_years"],
                   "support_pct": A["support_pct_of_acquisition"], "tb_per_admin_fte": A["tb_per_admin_fte"],
                   "admin_fte_loaded_cost": A["admin_fte_loaded_cost"], "cloud_usd_per_gb_month": A["cloud_usd_per_gb_month"],
                   "storage_pct_it": sp, "it_pct_revenue": A["it_pct_revenue"][ind],
                   "cloud_spend_disclosed": cloud_disc.numeric_value if cloud_disc else None,
                   "it_spend_disclosed": it_e.numeric_value if it_e else None,
                   "rack_overhead_ru": A["rack_overhead_ru_per_rack"], "switch_support_pct": A["switch_support_pct"]},
        "provenance": used,
        "capacity": {"total_tb": total_tb, "basis": cap_basis, "methods": cap_methods, "by_tier": tb, "mix": mix_used},
        "pricing": {"usd_per_tb": price, "basis": price_basis},
        "tier_lines": lines,
        "breakdown": cat,
        "power_cooling": {"it_kwh_year": sum(l["kwh_year"] for l in lines.values()),
                          "facility_kwh_year": sum(l["kwh_year"] for l in lines.values()) * pue,
                          "power_usd": cat["power"], "cooling_usd": cat["cooling_facility"], "pue": pue, "usd_per_kwh": kwh},
        "estimates": {"bottom_up": bottom_up if total_tb else None, "top_down": top_down,
                      "disclosed_spend": disc_spend.numeric_value if disc_spend else None,
                      "methods_used": {k: {"value": v, "weight": w} for k, (v, w) in methods.items()},
                      "annual_storage_spend": est, "range": rng, "confidence": confidence},
        "environment": {"primary_vendor": primary_vendor, "vendors_ranked": vendors,
                        "items": _env_items(evidence)},
        "readiness": readiness(profile, evidence),
    }


READINESS = [  # (input, fields, critical, has_default)
    ("Incumbent primary vendor", ["primary_storage_vendor", "storage_vendor"], True, False),
    ("Incumbent models / platforms", ["storage_model", "storage_product"], True, False),
    ("Install year / refresh timing", ["storage_install_year", "storage_refresh_plan", "support_contract_end"], True, False),
    ("Total capacity", ["capacity_tb_total", "capacity_tb_performance", "capacity_tb_general", "capacity_tb_capacity"], True, True),
    ("Block / file / object split", ["capacity_tb_block", "capacity_tb_file", "capacity_tb_object"], True, True),
    ("Data-reduction ratio", ["data_reduction_ratio", "incumbent_drr"], False, True),
    ("Usable % / utilisation", ["usable_pct", "utilization_pct"], False, True),
    ("Data growth rate", ["data_growth_pct"], False, True),
    ("Data-centre count / locations", ["data_center_count", "data_center_location"], True, True),
    ("Hosting model (owned vs colo)", ["colocation_provider"], False, False),
    ("PUE", ["pue"], True, True),
    ("Electricity price", ["usd_per_kwh"], False, True),
    ("Space cost ($/RU/month)", ["colocation_usd_per_ru_month"], False, True),
    ("Rack units / density", ["storage_rack_units", "incumbent_tb_per_ru"], False, True),
    ("Incumbent power (W/TB)", ["incumbent_watts_per_tb"], True, True),
    ("Networking footprint", ["network_switch_count"], False, True),
    ("WACC / discount rate", ["wacc_pct"], True, True),
    ("Revenue", ["revenue_usd"], True, False),
    ("Scope 2 / grid carbon factor", ["scope2_emissions_tco2e", "grid_kgco2e_per_kwh"], False, True),
    ("Strategic priorities (AI, DC moves…)", ["business_priority", "ai_initiative", "storage_initiative"], True, False),
    ("Stated IT pain points", ["it_pain_point"], True, False),
]


def readiness(profile: CompanyProfile, evidence: list[Evidence]) -> list[dict]:
    out = []
    for label, fields, critical, has_default in READINESS:
        ev = [e for e in evidence if e.field in fields and e.trust_tier > 0]
        if label == "Revenue" and not ev and profile.revenue_usd:
            out.append({"input": label, "status": "found", "detail": "company profile", "critical": critical, "evidence": []}); continue
        if ev:
            top = max(ev, key=_score)
            st = "found (verified)" if any(e.verified for e in ev) else "found (unverified)"
            out.append({"input": label, "status": st, "detail": f"{top.value[:70]} [{top.id}, T{top.trust_tier}]",
                        "critical": critical, "evidence": [e.id for e in ev]})
        else:
            out.append({"input": label, "status": "assumed" if has_default else "missing",
                        "detail": "model default - confirm with customer" if has_default else "not found in public sources",
                        "critical": critical, "evidence": []})
    return out


def _env_items(evidence):
    keep = ("primary_storage_vendor", "storage_vendor", "storage_product", "storage_model", "storage_protocol",
            "backup_vendor", "cloud_provider", "hci_vendor", "data_center_location", "colocation_provider",
            "storage_initiative", "data_center_count", "pue", "storage_install_year", "storage_refresh_plan",
            "support_contract_end", "incumbent_watts_per_tb", "incumbent_tb_per_ru", "incumbent_drr",
            "data_reduction_ratio", "storage_rack_units")
    rows = [e for e in evidence if e.field in keep and e.trust_tier > 0]
    rows.sort(key=lambda e: (keep.index(e.field), -_score(e)))
    return [{"field": e.field, "value": e.value, "tier": e.tier_hint, "trust_tier": e.trust_tier,
             "verified": e.verified, "source": e.source_url, "evidence_id": e.id} for e in rows]
