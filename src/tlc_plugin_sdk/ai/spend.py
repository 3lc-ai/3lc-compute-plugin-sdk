# Copyright 2026 3LC Inc.
# SPDX-License-Identifier: Apache-2.0
"""Transactional, deployment-wide admission and usage accounting in integer microdollars.

Reservations survive crashes. An ambiguous provider outcome retains its ceiling;
it is never treated as free usage. A settlement is idempotent and belongs to the
UTC day on which the request was reserved, even across midnight.
"""

from __future__ import annotations

import datetime
import json
import math
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Literal


class BudgetExceeded(Exception):
    """A call cannot be admitted within the saved limits."""


class BudgetLimitExceeded(BudgetExceeded):
    """An atomic admission refusal, with amounts from the same ledger transaction.

    ``used_usd`` includes actual spend and unsettled reservations. ``requested_usd``
    is the next call's conservative ceiling, not a charge already incurred.
    ``turn`` denotes the caller's message or plugin job, as chosen by the host.
    """

    def __init__(self, scope: Literal["turn", "day"], used_usd: float, requested_usd: float, limit_usd: float) -> None:
        self.scope = scope
        self.used_usd, self.requested_usd, self.limit_usd = used_usd, requested_usd, limit_usd
        super().__init__(
            "This message would exceed the saved limit per message"
            if scope == "turn"
            else "Today's assistant spend and pending calls would exceed the saved limit per day"
        )


def _micros(value: float) -> int:
    if not math.isfinite(value) or value < 0:
        msg = "Spend must be finite and non-negative"
        raise ValueError(msg)
    return math.ceil(value * 1_000_000)


class Ledger:
    def __init__(self, state_dir: Path) -> None:
        self.root = Path(state_dir) / "spend"
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.root.chmod(0o700)
        self.path = self.root / "usage.sqlite3"
        with self._connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("""CREATE TABLE IF NOT EXISTS usage (
                id TEXT PRIMARY KEY, user TEXT NOT NULL, turn TEXT NOT NULL,
                day TEXT NOT NULL, ceiling INTEGER NOT NULL, actual INTEGER,
                metadata TEXT NOT NULL DEFAULT '{}')""")
            db.execute("CREATE INDEX IF NOT EXISTS usage_user_day ON usage(user, day)")
        self.path.chmod(0o600)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=30)
        try:
            db.execute("PRAGMA busy_timeout=30000")
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def today() -> str:
        return datetime.datetime.now(datetime.UTC).date().isoformat()

    def _migrate(self, db: sqlite3.Connection, user_id: str) -> None:
        # Only the old collision-free identifier spelling is eligible for import.
        if not user_id or len(user_id) > 120 or any(not (c.isascii() and (c.isalnum() or c in "._-")) for c in user_id):
            return
        path = self.root / f"{user_id}.json"
        if not path.exists():
            return
        try:
            data = json.loads(path.read_text())
            if not isinstance(data, dict):
                msg = "Invalid old ledger"
                raise ValueError(msg)
            for day, value in data.items():
                datetime.date.fromisoformat(day)
                amount = _micros(float(value))
                db.execute(
                    "INSERT OR IGNORE INTO usage VALUES (?, ?, '', ?, ?, ?, '{}')",
                    (f"legacy:{user_id}:{day}", user_id, day, amount, amount),
                )
        except (ValueError, TypeError, OSError):
            msg = "The existing spend ledger needs repair before another call can be billed"
            raise BudgetExceeded(msg) from None

    def spent_today(self, user_id: str) -> float:
        with self._connect() as db:
            self._migrate(db, user_id)
            value = db.execute(
                "SELECT COALESCE(SUM(COALESCE(actual, ceiling)),0) FROM usage WHERE user=? AND day=?",
                (user_id, self.today()),
            ).fetchone()[0]
        return float(value) / 1_000_000

    def reserve(
        self,
        user_id: str,
        turn: str,
        request_id: str,
        ceiling_usd: float,
        per_turn_usd: float,
        per_day_usd: float,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        ceiling = _micros(ceiling_usd)
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._migrate(db, user_id)
            previous = db.execute("SELECT user, turn, ceiling, actual FROM usage WHERE id=?", (request_id,)).fetchone()
            if previous is not None:
                if previous[:3] != (user_id, turn, ceiling):
                    msg = "Reservation conflicts with an existing request"
                    raise ValueError(msg)
                if previous[3] is not None:
                    msg = "This provider request already settled; it cannot be dispatched again"
                    raise ValueError(msg)
                return
            day = self.today()
            spent = db.execute(
                "SELECT COALESCE(SUM(COALESCE(actual, ceiling)),0) FROM usage WHERE user=? AND turn=?", (user_id, turn)
            ).fetchone()[0]
            daily = db.execute(
                "SELECT COALESCE(SUM(COALESCE(actual, ceiling)),0) FROM usage WHERE user=? AND day=?", (user_id, day)
            ).fetchone()[0]
            if spent + ceiling > _micros(per_turn_usd):
                error = BudgetLimitExceeded("turn", spent / 1_000_000, ceiling / 1_000_000, per_turn_usd)
                raise error
            if daily + ceiling > _micros(per_day_usd):
                error = BudgetLimitExceeded("day", daily / 1_000_000, ceiling / 1_000_000, per_day_usd)
                raise error
            db.execute(
                "INSERT INTO usage VALUES (?, ?, ?, ?, ?, NULL, ?)",
                (request_id, user_id, turn, day, ceiling, json.dumps(metadata or {}, allow_nan=False)),
            )

    def settle(self, user_id: str, request_id: str, usd: float, metadata: dict[str, Any] | None = None) -> None:
        actual = _micros(usd)
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT actual FROM usage WHERE id=? AND user=?", (request_id, user_id)).fetchone()
            if row is None:
                msg = "No reservation for this caller"
                raise ValueError(msg)
            if row[0] is not None:
                if row[0] != actual:
                    msg = "A settled request cannot be changed"
                    raise ValueError(msg)
                return
            # Record provider-reported overruns, never silently clamp to the estimate.
            db.execute(
                "UPDATE usage SET actual=?, metadata=? WHERE id=? AND user=?",
                (actual, json.dumps(metadata or {}, allow_nan=False), request_id, user_id),
            )

    def add(self, user_id: str, usd: float) -> float:
        key = uuid.uuid4().hex
        self.reserve(user_id, key, key, usd, max(usd, 1), 1e12)
        self.settle(user_id, key, usd)
        return self.spent_today(user_id)


class TurnBudget:
    def __init__(
        self, ledger: Ledger, user_id: str, per_turn_usd: float, per_day_usd: float, turn_id: str | None = None
    ) -> None:
        self.ledger, self.user_id = ledger, user_id
        self.per_turn_usd, self.per_day_usd = per_turn_usd, per_day_usd
        self.turn_id = turn_id or uuid.uuid4().hex
        self.spent = 0.0
        self.pending: str | None = None

    def admit(self, ceiling_usd: float) -> None:
        if self.pending is not None:
            msg = "A model request is already pending"
            raise ValueError(msg)
        request_id = uuid.uuid4().hex
        self.ledger.reserve(self.user_id, self.turn_id, request_id, ceiling_usd, self.per_turn_usd, self.per_day_usd)
        self.pending = request_id

    def settle(self, usd: float) -> None:
        if self.pending is None:
            msg = "No pending model request"
            raise ValueError(msg)
        self.ledger.settle(self.user_id, self.pending, usd)
        self.spent += usd
        self.pending = None
