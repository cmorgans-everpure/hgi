"""Data models shared by tools, engine and exporters."""
from __future__ import annotations
from datetime import datetime
from typing import Literal, Optional
from pydantic import BaseModel, Field

Tier = Literal["performance", "general", "capacity", "archive", "cloud"]

# Evidence fields the engine understands. Descriptive fields feed the Environment sheet.
NUMERIC_FIELDS = {
    "revenue_usd", "employees", "it_spend_usd", "storage_spend_usd", "capex_usd",
    "capacity_tb_total", "capacity_tb_performance", "capacity_tb_general",
    "capacity_tb_capacity", "capacity_tb_archive", "capacity_tb_cloud",
    "cloud_storage_spend_usd", "data_center_count", "data_center_energy_mwh", "pue",
    "usd_per_kwh",
    # --- current-state value-prop inputs ---
    "capacity_tb_block", "capacity_tb_file", "capacity_tb_object",
    "data_reduction_ratio", "usable_pct", "utilization_pct", "data_growth_pct",
    "storage_install_year", "storage_rack_units", "colocation_usd_per_ru_month",
    "network_switch_count", "wacc_pct", "scope2_emissions_tco2e", "grid_kgco2e_per_kwh",
    "incumbent_watts_per_tb", "incumbent_tb_per_ru", "incumbent_drr",
}
DESCRIPTIVE_FIELDS = {
    "industry", "hq_region", "primary_storage_vendor", "storage_vendor", "storage_product",
    "storage_model", "storage_protocol", "backup_vendor", "cloud_provider", "hci_vendor",
    "data_center_location", "colocation_provider", "storage_initiative", "other",
    "ai_initiative", "it_pain_point", "business_priority", "storage_refresh_plan",
    "support_contract_end", "sustainability_target",
}
ALL_FIELDS = NUMERIC_FIELDS | DESCRIPTIVE_FIELDS


class Evidence(BaseModel):
    id: str
    company: str
    field: str
    value: str
    numeric_value: Optional[float] = None
    unit: Optional[str] = None
    tier_hint: Optional[Tier] = None          # which storage tier this relates to, if any
    source_url: str
    source_type: str
    source_title: Optional[str] = None
    quote: str                                 # verbatim supporting text
    published_date: Optional[str] = None
    trust_tier: int = 4
    trust_weight: float = 0.25
    verified: Optional[bool] = None            # None = not checked
    verification_note: Optional[str] = None
    found_by: Optional[str] = None
    recorded_at: str = Field(default_factory=lambda: datetime.utcnow().isoformat(timespec="seconds"))


class CompanyProfile(BaseModel):
    name: str
    ticker: Optional[str] = None
    domain: Optional[str] = None
    industry: str = "default"
    region: str = "US"
    revenue_usd: Optional[float] = None
    employees: Optional[float] = None
    fiscal_year: Optional[str] = None
    notes: Optional[str] = None
