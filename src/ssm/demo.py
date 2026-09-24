"""Offline demo: replays fictional evidence through the real engine (no API key / network needed)."""
import json
from . import DATA, state as S
from .schema import CompanyProfile, Evidence
from .sources import classify
from .agent import finalize


def run_demo(on_event=None) -> dict:
    d = json.loads((DATA / "demo_evidence.json").read_text())
    st = S.RunState(d["profile"]["name"], domain=d["profile"]["domain"], on_event=on_event, include_samples=True)
    S.STATE = st
    st.profile = CompanyProfile(**d["profile"])
    st.emit("status", message="DEMO MODE - fictional data, no web research performed")
    st.emit("profile", profile=st.profile.model_dump())
    for raw in d["evidence"]:
        tier, w, _ = classify(raw["source_url"], raw["source_type"], st.profile.domain)
        ev = Evidence(id=st.next_id(), company=st.profile.name, trust_tier=tier, trust_weight=w,
                      **{k: v for k, v in raw.items()})
        st.evidence.append(ev)
        st.emit("step", message=f"Recording {ev.field}: {ev.value}", sub=True)
        st.emit("evidence", evidence=ev.model_dump())
    st.notes = d["notes"]
    return finalize(st)
