"""Shared, transient consultation limits; these objects grant no write authority."""

from __future__ import annotations

import secrets
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType

from synapse.v2_contracts import V2Error


@dataclass(frozen=True)
class Preset:
    max_minutes: int
    max_operations: int
    max_source_expansions: int
    max_result_characters: int


PRESETS = MappingProxyType({
    "consult": Preset(2, 8, 8, 8000),
    "focused": Preset(10, 8, 8, 8000),
    "broad": Preset(20, 20, 20, 12000),
})
FREE_OPERATIONS = frozenset({"describe", "schema"})
EXPANSION_OPERATIONS = frozenset({"source", "passage", "research"})
SESSION_CAPACITY = 128
RECEIPT_GRACE_SECONDS = 300


def preset_budget(preset: str) -> dict:
    if not isinstance(preset, str) or preset not in PRESETS:
        raise V2Error("invalid-request", "Unknown request budget preset")
    limits = PRESETS[preset]
    return {"preset": preset, "max_minutes": limits.max_minutes,
            "max_source_expansions": limits.max_source_expansions,
            "max_result_characters": limits.max_result_characters}


class ConsultationBudget:
    """Reserve costs before validation/I/O. Failed admitted calls are not refunded.

    A denied call performs no read and cannot increase counters past their limits.
    Elapsed time is monotonic; reading a receipt never renews the deadline.
    """

    def __init__(self, preset="consult", *, limits: Mapping | None = None,
                 clock: Callable[[], float] | None = None):
        selected = preset_budget(preset)
        if limits is not None:
            if set(limits) != set(selected) or limits.get("preset") != preset:
                raise V2Error("invalid-request", "Budget must match the selected preset")
            for key in ("max_minutes", "max_source_expansions", "max_result_characters"):
                value = limits[key]
                ceiling = 32000 if key == "max_result_characters" else selected[key]
                minimum = 1000 if key == "max_result_characters" else 1
                if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= ceiling:
                    raise V2Error("invalid-request", f"{key} exceeds the selected bounded preset")
            selected = dict(limits)
        self.limits = MappingProxyType(selected)
        self.max_operations = PRESETS[preset].max_operations
        self._clock = clock or time.monotonic
        self.started = self._clock()
        self.deadline = self.started + selected["max_minutes"] * 60
        self._lock = threading.RLock()
        self._calls = self._expansions = 0
        self._closed_at: float | None = None

    def charge(self, operation: str) -> int:
        """Atomically reserve one attempted operation and its initial expansion."""
        with self._lock:
            if self._closed_at is not None:
                raise V2Error("precondition-expired", "Consultation has ended")
            if operation in FREE_OPERATIONS:
                return 0
            if self._clock() >= self.deadline or self._calls >= self.max_operations:
                raise V2Error("coverage-limited", "Consultation retrieval budget exhausted; synthesize the evidence already read")
            self._calls += 1
            expansion = int(operation in EXPANSION_OPERATIONS)
            if expansion and self._expansions >= self.limits["max_source_expansions"]:
                raise V2Error("coverage-limited", "Source expansion budget exhausted; synthesize the evidence already read")
            self._expansions += expansion
            return expansion

    def charge_research_pages(self, response: Mapping) -> int:
        """Settle extra pages/failures after research's initial reserved expansion.

        The trusted research callback must honor the supplied source_limit. If it
        violates that contract, saturate the budget and reject its response.
        """
        cost = max(1, len(response.get("pages", [])) + len(response.get("failures", [])))
        with self._lock:
            total = self._expansions + cost - 1
            self._expansions = min(total, self.limits["max_source_expansions"])
            if total > self.limits["max_source_expansions"]:
                raise V2Error("coverage-limited", "Research callback exceeded its source limit")
        return cost

    def receipt(self) -> dict:
        with self._lock:
            now = self._clock() if self._closed_at is None else self._closed_at
            return {"elapsed_seconds": max(0, int(now - self.started)),
                    "calls": self._calls, "expansions": self._expansions,
                    "remaining": {"calls": self.max_operations - self._calls,
                                  "expansions": self.limits["max_source_expansions"] - self._expansions,
                                  "seconds": max(0, int(self.deadline - now))}}

    def remaining(self) -> dict:
        remaining = self.receipt()["remaining"]
        return {"operations": remaining["calls"], "source_expansions": remaining["expansions"],
                "seconds": remaining["seconds"]}

    def close(self) -> dict:
        with self._lock:
            if self._closed_at is None:
                self._closed_at = self._clock()
            return self.receipt()


@dataclass
class ConsultationSession:
    vault: Path
    revision: str
    timezone: str
    known_at: str | None
    budget: ConsultationBudget
    expires_at: float
    source_purpose_hash: str | None = None
    lock: threading.RLock = field(default_factory=threading.RLock)


class ConsultationSessions:
    """Process-local bounded registry, storing no questions, evidence or run logs.

    Keep a session lock across charge, dispatch and receipt so concurrent reads
    and end cannot race. Registry lookup never holds its lock while waiting on
    a session. Tokens are bearer references to read budgets, never capabilities.
    """

    def __init__(self, *, capacity=SESSION_CAPACITY, clock: Callable[[], float] | None = None):
        if isinstance(capacity, bool) or not isinstance(capacity, int) or not 1 <= capacity <= SESSION_CAPACITY:
            raise ValueError("Session capacity must be between 1 and 128")
        self.capacity = capacity
        self._clock = clock or time.monotonic
        self._sessions: dict[str, ConsultationSession] = {}
        self._lock = threading.RLock()

    def _prune(self) -> None:
        now = self._clock()
        for token in [key for key, session in self._sessions.items() if now >= session.expires_at]:
            del self._sessions[token]

    def begin(self, vault: Path, revision: str, *, preset="consult", timezone=None,
              known_at=None, source_purpose_hash=None) -> tuple[str, ConsultationSession]:
        from synapse.config import resolve_timezone
        timezone = resolve_timezone(vault, timezone)
        budget = ConsultationBudget(preset, clock=self._clock)
        session = ConsultationSession(Path(vault).resolve(), revision, timezone, known_at, budget,
                                      budget.deadline + RECEIPT_GRACE_SECONDS, source_purpose_hash)
        with self._lock:
            self._prune()
            if len(self._sessions) >= self.capacity:
                raise V2Error("coverage-limited", "Consultation session capacity reached; end an existing session")
            token = secrets.token_urlsafe(32)
            while token in self._sessions:
                token = secrets.token_urlsafe(32)
            self._sessions[token] = session
        return token, session

    def get(self, token: str) -> ConsultationSession:
        with self._lock:
            self._prune()
            if not isinstance(token, str) or len(token) != 43 or token not in self._sessions:
                raise V2Error("precondition-expired", "Unknown, ended or expired consultation session")
            return self._sessions[token]

    def end(self, token: str, session: ConsultationSession) -> dict:
        with session.lock:
            receipt = session.budget.close()
            with self._lock:
                if self._sessions.get(token) is session:
                    del self._sessions[token]
            return receipt
