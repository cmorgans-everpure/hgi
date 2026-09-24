"""Orchestrator + research sub-agents built on the Claude Agent SDK."""
from __future__ import annotations
import os
from datetime import datetime
from claude_agent_sdk import (AgentDefinition, AssistantMessage, ClaudeAgentOptions, ClaudeSDKClient,
                              ResultMessage, TextBlock, ToolUseBlock)
from . import OUTPUT, state as S
from .tools import SERVER, T
from . import model as engine
from .reconcile import reconcile
from .export import to_excel, to_json

ORCH_MODEL = os.getenv("SSM_MODEL", "claude-opus-5-5")
SUB_MODEL = os.getenv("SSM_SUBAGENT_MODEL", "claude-sonnet-5")

EVIDENCE_RULES = """
EVIDENCE RULES (non-negotiable):
- Every fact goes through mcp__ssm__record_evidence with a VERBATIM quote copied from the page you fetched.
  Never paraphrase in `quote`. Never record something you inferred - only what a source states.
- Always WebFetch the page before recording; search-result snippets are not sources.
- Prefer, in order: company filings/annual/ESG reports > vendor case studies & press releases, government contracts >
  reputable trade press & job postings. Forums, Wikipedia, Reddit, Medium are rejected by the tool.
- Confirm the source is about THIS company (not a similarly named firm; note subsidiaries explicitly in `value`).
- Include published_date. Prefer sources from the last 3 years; older ones are down-weighted automatically.
- Numeric values in base units: USD (not $M), TB (1 PB = 1000 TB), MWh, counts.
- Record one fact per call. It is fine to find nothing for a field - do NOT guess.
"""

FIN = AgentDefinition(
    description="Financial & corporate researcher: SEC/annual reports, earnings calls, IT spend, data-centre footprint.",
    prompt=f"""You research a company's financial and corporate IT footprint for a storage spend model.
Find: revenue_usd, employees, it_spend_usd (only if disclosed), capex_usd, data_center_count, cloud_provider
(multi-year cloud commitments), storage_initiative (data-centre consolidation, cloud migration, AI/data platform build-outs),
data_center_location, colocation_provider, and any storage_spend_usd disclosure.
Also gather the value-proposition narrative & finance inputs:
 - wacc_pct: the discount rate / WACC disclosed in the goodwill-impairment note of the annual report (decimal, e.g. 0.085;
   say pre- or post-tax in `value`).
 - business_priority and ai_initiative: stated strategic priorities (AI, new data centres, cloud exit/repatriation,
   M&A integration, cost programmes) from the CEO letter, 10-K Item 1/7 or earnings calls.
 - it_pain_point: explicitly stated IT problems (legacy infrastructure, cyber incidents/ransomware, outages, capacity
   constraints, cost pressure, skills shortage) - 10-K risk factors, 8-K cyber disclosures, earnings calls.
 - data_growth_pct if the company states data growth (decimal).
Tools: for US-listed companies call mcp__ssm__sec_company_facts first, then mcp__ssm__sec_full_text_search for
terms like "data center", "storage", "cloud", vendor names; open the filing with WebFetch. For non-US companies use
the annual report / 20-F on the investor-relations site. Earnings-call transcripts only from the company IR site.
{EVIDENCE_RULES}
Set found_by="financial". Finish with a 5-line summary of what you found and what you could not find.""",
    tools=["WebSearch", "WebFetch"] + T("record_evidence", "sec_company_facts", "sec_full_text_search"),
    model=SUB_MODEL)

INFRA = AgentDefinition(
    description="Infrastructure researcher: storage vendors, products, models, capacity, tiers, backup, cloud storage.",
    prompt=f"""You identify a company's storage environment.
Find: primary_storage_vendor, storage_vendor (one record per vendor), storage_product / storage_model (e.g. "PowerMax 8500",
"AFF A800", "FlashArray//X"), storage_protocol (FC SAN, NVMe-oF, NFS, S3), backup_vendor, hci_vendor, cloud_provider,
capacity_tb_total or capacity_tb_<tier> (performance=AFA/NVMe, general=QLC/hybrid, capacity=HDD/object/scale-out NAS,
archive=tape/cold, cloud), cloud_storage_spend_usd.
Current-state detail for the value proposition (record whatever is stated):
 - capacity_tb_block / capacity_tb_file / capacity_tb_object (protocol split), data_reduction_ratio, usable_pct,
   utilization_pct (decimals).
 - storage_install_year (year a named array/platform was deployed - case-study or press dates count),
   storage_refresh_plan (announced refresh/migration/tender), support_contract_end (e.g. period of performance in
   government contracts), storage_rack_units, network_switch_count.
 - Incumbent product specs from the INCUMBENT vendor's own datasheets / spec pages for the identified models:
   incumbent_watts_per_tb (typical W / effective or usable TB - say which), incumbent_tb_per_ru (usable TB per rack unit),
   incumbent_drr (vendor-guaranteed data-reduction ratio - mark as vendor claim in `value`). Use source_type
   vendor_press_release or company_website of the vendor; these describe the product, not the customer.
Where to look:
 1. Vendor customer case studies & press releases (site:purestorage.com, netapp.com, dell.com, hpe.com, ibm.com,
    hitachivantara.com, vastdata.com, weka.io, nutanix.com, cohesity.com, rubrik.com, veeam.com, commvault.com,
    aws.amazon.com/solutions/case-studies, azure.microsoft.com, cloud.google.com/customers).
 2. Government procurement if public sector (usaspending.gov, sam.gov, contractsfinder / ted.europa.eu).
 3. Job postings (company careers pages, greenhouse, lever, workday, linkedin jobs) naming storage platforms -
    record as storage_vendor/storage_product with source_type=job_posting (these are tech-stack signals, weaker).
 4. Trade press (blocksandfiles.com, theregister.com, computerweekly.com, datacenterdynamics.com) and conference talks.
Use mcp__ssm__idc_price_lookup to check model names match IDC nomenclature.
Mark primary_storage_vendor only if a source indicates the vendor is the main/strategic primary-storage platform.
{EVIDENCE_RULES}
Set found_by="infrastructure". Finish with a summary: vendors found, strength of each signal, gaps.""",
    tools=["WebSearch", "WebFetch"] + T("record_evidence", "idc_price_lookup"),
    model=SUB_MODEL)

ESG = AgentDefinition(
    description="Sustainability researcher: data-centre energy (MWh), PUE, locations, electricity pricing.",
    prompt=f"""You research data-centre energy & facilities to derive power and cooling costs.
Find in ESG / sustainability / CDP / TCFD reports or the company site: data_center_energy_mwh (data-centre-specific, not
whole-company, unless clearly labelled - say which in `value`), pue, data_center_count, data_center_location,
colocation_provider, usd_per_kwh if disclosed. Also cloud_provider sustainability partnerships.
Also: scope2_emissions_tco2e (market- or location-based - say which), grid_kgco2e_per_kwh if disclosed,
sustainability_target (e.g. net-zero year, renewable %, data-centre efficiency goals), colocation_usd_per_ru_month if a
colo contract price is public.
PDF reports are fine - WebFetch the PDF and quote the exact line.
{EVIDENCE_RULES}
Set found_by="sustainability". Finish with a short summary and gaps.""",
    tools=["WebSearch", "WebFetch"] + T("record_evidence"),
    model=SUB_MODEL)

ORCH_PROMPT = """You are the lead analyst for Everpure's storage-spend modelling. You produce an independent,
source-backed estimate of a company's annual storage spend and environment, which is later compared against HGI and
IDC data (you will NOT see HGI; stay independent).

Workflow:
1. Resolve identity: WebSearch/WebFetch the company's official site / IR page. Determine legal name, ticker (if any),
   primary domain, industry (use the enum), HQ region. If US-listed call mcp__ssm__sec_company_facts.
   Call mcp__ssm__set_company_profile (include revenue_usd / employees if you have sourced figures).
   If the name is ambiguous, pick the most likely large enterprise and say so in notes.
2. Launch the three research sub-agents IN PARALLEL with the Agent tool (financial, infrastructure, sustainability).
   Give each: company legal name, domain, ticker, industry, and any known subsidiaries.
3. Call mcp__ssm__verify_evidence (no args). For failed items that matter (capacity, primary vendor, energy), try to
   find a replacement source yourself, or accept it as unverified.
4. Call mcp__ssm__evidence_summary. If key fields are missing, do ONE targeted gap-filling pass (yourself or one
   sub-agent), then verify again.
   Value-prop critical inputs to chase: capacity & protocol split, incumbent vendor + models + install year,
   data_center_count / locations, pue, wacc_pct, business_priority / it_pain_point.
5. Call mcp__ssm__build_model. Review its `readiness` list and try once more for any 'missing' critical item. Sanity-check: does spend as % of revenue look plausible for the industry? Does the tier
   mix match the vendors found (e.g. mostly AFA vendors => performance-heavy)? If the capacity basis is only heuristic,
   say so.
6. End with ANALYST NOTES (<=180 words, plain text): headline estimate & range, what drives it, strongest and weakest
   evidence, the 3 things a seller should validate with the customer.
Never invent numbers. Do not write files."""


def _desc(block: ToolUseBlock) -> str:
    i = block.input or {}
    n = block.name.replace("mcp__ssm__", "")
    if n in ("Agent", "Task"): return f"Launching sub-agent: {i.get('subagent_type') or i.get('description', '')}"
    if n == "WebSearch": return f"Searching: {i.get('query', '')}"
    if n == "WebFetch": return f"Reading: {i.get('url', '')}"
    if n == "record_evidence": return f"Recording {i.get('field')}: {str(i.get('value'))[:80]}"
    return f"{n} {str({k: v for k, v in i.items() if k in ('ticker', 'query', 'vendor', 'model', 'name')})}"


async def run(company: str, ticker: str | None = None, domain: str | None = None,
              on_event=None, include_samples: bool = False, max_turns: int = 80) -> dict:
    st = S.RunState(company, ticker, domain, on_event, include_samples)
    S.STATE = st
    st.emit("status", message=f"Starting research on {company}")
    opts = ClaudeAgentOptions(
        system_prompt=ORCH_PROMPT, model=ORCH_MODEL,
        mcp_servers={"ssm": SERVER},
        agents={"financial": FIN, "infrastructure": INFRA, "sustainability": ESG},
        allowed_tools=["Agent", "Task", "WebSearch", "WebFetch"] + T(
            "set_company_profile", "record_evidence", "verify_evidence", "evidence_summary", "sec_company_facts",
            "sec_full_text_search", "idc_price_lookup", "build_model"),
        disallowed_tools=["Bash", "Write", "Edit", "NotebookEdit"],
        permission_mode="bypassPermissions", max_turns=max_turns, setting_sources=[])
    hint = f"Company: {company}" + (f"\nTicker: {ticker}" if ticker else "") + (f"\nDomain: {domain}" if domain else "")
    final_text = []
    async with ClaudeSDKClient(options=opts) as client:
        await client.query(f"Build the storage spend model.\n{hint}")
        async for msg in client.receive_response():
            if isinstance(msg, AssistantMessage):
                sub = getattr(msg, "parent_tool_use_id", None) is not None
                for b in msg.content:
                    if isinstance(b, ToolUseBlock):
                        st.emit("step", message=_desc(b), sub=sub)
                    elif isinstance(b, TextBlock) and not sub:
                        final_text.append(b.text)
            elif isinstance(msg, ResultMessage):
                st.emit("status", message=f"Agent finished ({msg.num_turns} turns, ${msg.total_cost_usd or 0:.2f})")
    st.notes = final_text[-1] if final_text else ""
    return finalize(st)


def finalize(st: "S.RunState") -> dict:
    """Deterministic post-processing: model, reconciliation vs HGI/IDC, exports."""
    m = engine.build(st.profile, st.evidence, include_samples=st.include_samples)
    rc = reconcile(m, include_samples=st.include_samples)
    ev = [e.model_dump() for e in st.evidence]
    slug = "".join(ch if ch.isalnum() else "_" for ch in st.profile.name).strip("_")[:40]
    stamp = datetime.now().strftime("%Y%m%d_%H%M")
    xlsx = to_excel(m, rc, ev, OUTPUT / f"{slug}_{stamp}.xlsx")
    js = to_json(m, rc, ev, OUTPUT / f"{slug}_{stamp}.json")
    result = {"model": m, "reconciliation": rc, "evidence": ev, "notes": st.notes,
              "files": {"xlsx": xlsx.name, "json": js.name}}
    st.emit("done", result=result)
    return result
