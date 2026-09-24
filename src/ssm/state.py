"""Per-run state shared by the agent tools (one research run at a time)."""
from __future__ import annotations
import itertools
from typing import Callable, Optional
from .schema import CompanyProfile, Evidence


class RunState:
    def __init__(self, company: str, ticker: str | None = None, domain: str | None = None,
                 on_event: Optional[Callable[[dict], None]] = None, include_samples: bool = False):
        self.profile = CompanyProfile(name=company, ticker=ticker, domain=domain)
        self.evidence: list[Evidence] = []
        self._ids = itertools.count(1)
        self.on_event = on_event or (lambda e: None)
        self.include_samples = include_samples
        self.model: dict | None = None
        self.notes: str = ""

    def next_id(self) -> str:
        return f"E{next(self._ids):03d}"

    def emit(self, kind: str, **data):
        self.on_event({"type": kind, **data})


STATE: RunState | None = None
