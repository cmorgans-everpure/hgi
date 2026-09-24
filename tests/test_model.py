import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))
from ssm.sources import classify
from ssm.demo import run_demo
from ssm.loaders import hgi_rows, idc_tier_prices


def test_source_tiers():
    assert classify("https://www.sec.gov/Archives/x", "10-K")[0] == 1
    assert classify("https://www.purestorage.com/customers/x", "vendor_case_study")[0] == 2
    assert classify("https://blocksandfiles.com/2025/x", "trade_press")[0] == 3
    assert classify("https://www.reddit.com/r/storage", "other")[0] == 0
    assert classify("https://en.wikipedia.org/wiki/X", "other")[0] == 0
    assert classify("https://careers.acme.com/job", "job_posting")[0] == 3
    assert classify("https://acme.com/about", "company_website", "acme.com")[0] == 1


def test_loaders():
    assert len(hgi_rows("Example Corp.", include_samples=True)) == 5
    assert hgi_rows("Example Corp") == []            # samples excluded by default
    p = idc_tier_prices(["Dell Technologies"], include_samples=True)
    assert p["performance"]["usd_per_tb"] == 1150 and "vendor-matched" in p["performance"]["basis"]


def test_demo_end_to_end():
    r = run_demo()
    m = r["model"]; e = m["estimates"]
    assert abs(m["capacity"]["total_tb"] - 95000) < 1        # disclosed capacity used
    assert m["capacity"]["basis"] == "disclosed"
    assert m["environment"]["primary_vendor"] == "Dell Technologies"
    assert m["power_cooling"]["pue"] == 1.45
    bu = sum(m["breakdown"].values())
    assert abs(bu - e["bottom_up"]) < 1
    lo, hi = e["range"]; assert lo <= e["annual_storage_spend"] <= hi
    assert r["reconciliation"]["hgi_storage_total"] == 9_500_000   # networking row excluded
    assert "Dell Technologies" in r["reconciliation"]["vendors"]["confirmed_by_hgi"]


def test_baseline_and_readiness():
    m = run_demo()["model"]
    b = m["baseline"]
    assert len(b["totals_by_year"]) == 5 and b["total"] > 5 * sum(m["breakdown"].values()) * 0.99
    assert abs(b["totals_by_year"][0] - sum(m["breakdown"].values())) < 1       # year 1 == annual run-rate
    assert m["breakdown"]["data_center_space"] > 0 and m["breakdown"]["networking"] > 0
    assert m["finance_esg"]["wacc_pct"] == 0.092
    assert m["tier_lines"]["performance"]["watts_per_tb"] == 4.1                 # incumbent spec used
    assert abs(m["capacity_profile"]["by_protocol"]["file"]["usable_tb"] - 30000) < 1
    assert any(r["status"] == "DUE ≤12M" or r["status"] == "OVERDUE" for r in m["refresh"])   # 2021 + 5 = 2026
    st = {r["input"]: r["status"] for r in m["readiness"]}
    assert st["WACC / discount rate"].startswith("found") and st["Hosting model (owned vs colo)"] == "missing"
