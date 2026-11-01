"""Reveals per member per hour (design v3 §4.4: "an app-level 'reveals per member per hour' metric covers bulk
reveal"; AA-937, card CRM-09).

Field decryption is local AES under a cached DEK, so a member opening many records does not raise the KMS Decrypt
rate and no KMS alarm would see it. This meter counts what the app itself released instead: every committed
``reveal`` audit event with outcome ``allowed`` (a field reveal, a document download), per agency and member, over a
sliding hour. :func:`app.tenancy.tenant_tx` records them **after** the transaction committed, so a reveal that was
rolled back (its value never returned) is not counted.

- :meth:`RevealMeter.peak` is the highest count of any member in the last hour; the job sampler logs it on its
  ``ops_metrics_sampled`` line (S263: no longer a CloudWatch metric).
- Crossing :data:`BULK_REVEAL_THRESHOLD` in the hour logs one ``crm_bulk_reveal`` warning per member and crossing
  (agency id, member id and the count: random ids, no value) and raises the in-app check ``bulk_reveal``
  (:mod:`app.observability.ops_alerts`: one AA ops e-mail per member and hour; it replaced the S263-removed alarm).

Per process: one API task on Dev, so the count is exact there; with several tasks each task meters its own requests
(a lower bound). Rate limiting bulk reveal itself is a later card
(design §6 incident card: "bulk reveal above a threshold needs a reason and is rate-limited").
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable
from datetime import UTC, datetime

from app.observability import ops_alerts

log = logging.getLogger(__name__)

WINDOW_S = 3600.0
# A member who opens 60 fields or files in an hour is far beyond preparing a few bookings by hand.
BULK_REVEAL_THRESHOLD = 60
# Memory bound per member: past this many reveals in the window the count saturates (still far above the threshold).
MAX_PER_MEMBER = 10_000


class RevealMeter:
    def __init__(
        self,
        *,
        window_s: float = WINDOW_S,
        threshold: int = BULK_REVEAL_THRESHOLD,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._window_s = window_s
        self._threshold = threshold
        self._clock = clock
        self._lock = threading.Lock()
        self._events: dict[tuple[uuid.UUID, uuid.UUID], deque[float]] = {}

    def _prune(self, now: float) -> None:
        cutoff = now - self._window_s
        for key in list(self._events):
            stamps = self._events[key]
            while stamps and stamps[0] <= cutoff:
                stamps.popleft()
            if not stamps:
                del self._events[key]

    def record(self, tenant_id: uuid.UUID, member_id: uuid.UUID, count: int = 1) -> int:
        """Count ``count`` committed reveals of this member now; returns the member's count in the window."""
        if count <= 0:
            return self.count(tenant_id, member_id)
        with self._lock:
            now = self._clock()
            self._prune(now)
            stamps = self._events.setdefault((tenant_id, member_id), deque(maxlen=MAX_PER_MEMBER))
            before = len(stamps)
            stamps.extend([now] * count)
            after = len(stamps)
        if before < self._threshold <= after:
            log.warning(
                "crm_bulk_reveal tenant=%s member=%s reveals_last_hour=%d threshold=%d",
                tenant_id,
                member_id,
                after,
                self._threshold,
            )
            # S263 (AA-1025): one AA ops e-mail per member and hour (was the RevealsPerMemberPeakHour alarm).
            hour = datetime.now(UTC).strftime("%Y-%m-%dT%H")
            ops_alerts.emit(
                "bulk_reveal", f"{member_id}:{hour}", tenant_id=tenant_id, member_id=member_id, reveals_last_hour=after
            )
        return after

    def count(self, tenant_id: uuid.UUID, member_id: uuid.UUID) -> int:
        with self._lock:
            self._prune(self._clock())
            return len(self._events.get((tenant_id, member_id), ()))

    def peak(self) -> int:
        """The highest per-member count in the last hour (0 when nobody revealed anything)."""
        with self._lock:
            self._prune(self._clock())
            return max((len(s) for s in self._events.values()), default=0)

    def reset(self) -> None:
        with self._lock:
            self._events.clear()


# The process-wide meter (like a metrics registry): tenant_tx() records into it, the sampler reads it.
METER = RevealMeter()
