# hgi — Storage Spend Modeler

A Claude bot that mimics GVM: researches and models storage spend for the companies we work with.

A self-contained app built on the Claude Agent SDK. You enter a company name and it researches trusted public sources, then builds an independent storage spend model with citations. It checks that model against your HGI and IDC data.

## Quick start

```bash
cp .env.example .env        # add ANTHROPIC_API_KEY and SEC_USER_AGENT (name + email)
./run_app.sh                # first run creates .venv and installs deps, then opens http://127.0.0.1:8765
```

- Click **Demo** to try the full UI with fictional data. It needs no API key and does no web research.
- Upload your **HGI** and **IDC** CSV files from the app, or drop them in `data/hgi/` and `data/idc/`. If your column headers differ, edit `config/column_maps.yaml`.
- For a single command-line run: `PYTHONPATH=src .venv/bin/python -m ssm.cli "Company Name" --ticker XYZ`

Each run writes `output/<Company>_<timestamp>.xlsx` (the model, driven by formulas) and a `.json` file. Past runs can be reopened from the **Past runs** menu.

## Deploy for testing (Docker)

Needs Docker Desktop (Mac/Windows) or Docker Engine (Linux).

```bash
cp .env.example .env              # add ANTHROPIC_API_KEY and SEC_USER_AGENT
docker compose up -d --build      # builds and starts the app
open http://localhost:8765        # try Demo first, then a real company
docker compose logs -f            # watch agent progress / errors
docker compose down               # stop
```

- HGI and IDC CSVs go in `data/hgi/` and `data/idc/`, or you can upload them in the app. Generated models are saved in `output/`. All three folders are mounted into the container, so files survive rebuilds.
- By default the app is only reachable from your own machine. To let colleagues on your network test it, change the port line in `docker-compose.yml` to `"8765:8765"`. Only do this on a trusted network, because the app has no login yet.
- After `git pull`, run `docker compose up -d --build` to update.

## Working from git

- `.gitignore` keeps these out of the repo: `.env` (API keys), your real HGI and IDC CSVs (licensed data), and everything in `output/`. Only the fictional `SAMPLE_*.csv` files are committed.
- GitHub Actions (`.github/workflows/ci.yml`) runs the offline tests and a Docker build on every push.

## How it works

```
Orchestrator (Opus)  ── resolves identity, SEC XBRL financials
   ├─ financial agent       10-K/20-F, annual reports, IR earnings calls, EDGAR full-text search
   ├─ infrastructure agent  vendor case studies & PRs, gov procurement, job postings, trade press
   └─ sustainability agent  ESG/CDP reports: data-centre MWh, PUE, sites, colocation
        │  every fact → record_evidence (verbatim quote + URL + date, trust-tiered)
        ▼
 verify_evidence  re-fetches each source and confirms the quote is really there
        ▼
 Deterministic engine (model.py)  →  Reconciliation vs HGI & IDC (reconcile.py)  →  Excel / JSON / UI
```

**Evidence controls**

- Each source gets a trust tier: 1 for filings and company reports, 2 for vendor and government sources, 3 for press and job postings, and 4 for weak sources. Blocked domains such as Wikipedia, Reddit and Medium are rejected. You can edit the tiers in `config/trusted_sources.yaml`.
- Every fact must come with a quote copied word for word from the source. The app then fetches the source itself to check the quote. Evidence whose quote isn't found is marked down. Evidence from pages that block bots is shown as "unreachable".
- Older sources count for less: each year of age cuts a source's weight by 12%.
- **HGI is never shown to the agents.** This keeps the estimate independent, so the comparison with HGI means something.

**Model (all figures are annual)**

| View | Method |
|---|---|
| Bottom-up | For each tier: usable TB × $/TB (IDC, matched to the vendor where possible) ÷ refresh years, plus support, power (TB × W/TB × 8.76 × $/kWh), cooling (power × (PUE − 1)), cloud and admin labor |
| Top-down | Revenue × industry IT % × storage % of IT (low / mid / high), or disclosed IT spend |
| Energy check | ESG data-centre MWh ÷ PUE × storage share of IT energy, converted to implied capacity |
| Disclosed | Any storage spend the company itself discloses |

The views are combined as a weighted average into one estimate, with a range and a 0–100 confidence score. Confidence depends on how many verified tier 1–2 facts there are and how closely the views agree.

**Tiers:** performance (all-flash/NVMe) · general (QLC flash/hybrid) · capacity (HDD/object/scale-out) · archive (tape) · cloud.

**Reconciliation checks:** total spend vs the sum of HGI's storage categories · cloud storage spend · primary vendor · $/TB for each tier vs the IDC median · which vendors both sources found, which only research found, and which only HGI lists · capacity implied by HGI line items priced with IDC $/TB. Anything more than ±25% apart is flagged.

## Current-state inputs for value props (data gathering only)

The tool gathers the **incumbent's current state** that an Everpure value prop or proposal needs, laid out like the Arm business case. It never models Everpure products.

| Output | Contents |
|---|---|
| **Current-State Baseline** (5-yr) | Platform (HW, SW & support) · Networking · Power & Cooling · Data Centre Space · Admin labor · Cloud, by year with data growth; NPV at the company's WACC; storage kWh and tCO2e; rack units |
| **Capacity Profile** | Raw, usable and stored data for block, file and object, using the incumbent's data-reduction ratio, usable % and utilisation |
| **Refresh timing** | Install year, age and refresh-due date for each named system, plus announced refresh plans and support-contract end dates |
| **Incumbent specs** | W/TB, TB/RU and guaranteed data-reduction ratio from the incumbent vendor's datasheets. These values feed power and space costs |
| **Narrative hooks** | Strategic priorities, AI initiatives, stated IT pain points, DC initiatives and sustainability targets, all with sources |
| **Value-Prop Readiness** | Each business-case input marked *found (verified)*, *found*, *assumed* or *missing*. Critical inputs are flagged so sellers know what to confirm in discovery |

Finance and ESG inputs the agents look for: WACC (from annual-report impairment notes), Scope 2 emissions, grid carbon factor, PUE, $/kWh and colocation $/RU.

## Calibrate before trusting the numbers

The defaults in `config/assumptions.yaml` are reasonable starting values, not validated benchmarks. Check these against 5–10 accounts where you know the real answer:
`storage_pct_it_spend`, `it_pct_revenue`, `tb_per_employee`, `default_tier_mix`, `watts_per_tb` (add vendor spec-sheet values under `vendor_watts_per_tb`), `support_pct_of_acquisition`, `tb_per_admin_fte`, `tb_per_ru`, `incumbent_drr`, `protocol_mix`, `switch_price_usd`, `colocation_usd_per_ru_month` and `data_growth_pct`. IDC prices automatically replace the $/TB defaults wherever a row matches.

## Files

```
config/   assumptions.yaml · trusted_sources.yaml · column_maps.yaml
data/     hgi/*.csv · idc/*.csv · demo_evidence.json (fictional)
src/ssm/  app.py + ui.html (web app) · agent.py (orchestrator & sub-agents) · tools.py (MCP tools)
          model.py (engine) · reconcile.py · export.py · sources.py · loaders.py · cli.py · demo.py
tests/    pytest suite (runs offline)
```

## Notes

- The SEC APIs need `SEC_USER_AGENT` set to a real contact email.
- A full run takes about 10–25 minutes. Cost depends on the model settings (`SSM_MODEL`, `SSM_SUBAGENT_MODEL`).
- The app runs one research job at a time and uses local storage only. For team use, put it behind your SSO on an internal host.
- Public sources rarely state exact capacity or tier mix. When the capacity basis says `heuristic`, treat the estimate as directional and confirm it with the customer.
