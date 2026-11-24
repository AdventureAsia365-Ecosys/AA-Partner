"""When the Booking sync checks an agency's accepted proposals (S291, AA-838, C4.3 W48 finding 4).

:meth:`app.booking.sync.BookingSync.proposal_sets` reads the agency's accepted builder versions that still need their
AA lines (``proposal_lines.DUE_SQL``) and the confirms a paid deposit made due (``CONFIRM_DUE_SQL``). It ran for every
agency on every minute run: about one ``DUE_SQL`` call a second on Dev, nearly always finding nothing.

The step is event driven now:

- a **nudge run** (payload ``tenant_id``: an accept, a paid deposit, a claim extension, a re-hold queue one,
  :func:`app.booking.proposal_lines.nudge`) always checks;
- the **minute run** of every agency checks an agency only when its last check left work (a set was due, a confirm
  was queued or failed, Booking gave no answer, the check raised) or ``crm.jobs.booking_proposal_check_every_s``
  seconds (default 300) have passed since its last clean check: the backstop for a nudge that never ran.

The memory (:data:`CHECKS`: agency -> clock time of its last clean check) is per process; a new process checks every
agency on its first minute run.
"""

from __future__ import annotations

import time
import uuid
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import asyncpg

from app import platform_settings as ps

# The counters of the step that mean "work was due or is left": every due set moves one proposal_lines_* counter
# (written, retry, refused, error), and a confirm queued or failed has requests to follow.
WORK_PREFIX = "proposal_lines_"
WORK_KEYS = ("confirms_queued", "confirms_error")


def work_done(counts: Counter[str]) -> int:
    return sum(v for k, v in counts.items() if k.startswith(WORK_PREFIX) or k in WORK_KEYS)


@dataclass
class ProposalChecks:
    clock: Callable[[], float] = time.monotonic
    clean_at: dict[uuid.UUID, float] = field(default_factory=dict)

    def due(self, tenant_id: uuid.UUID, *, nudged: bool) -> bool:
        last = self.clean_at.get(tenant_id)
        if nudged or last is None:
            return True
        return self.clock() - last >= float(ps.get(ps.JOBS_PROPOSAL_CHECK_EVERY_S))

    async def run(
        self, sync: Any, pool: asyncpg.Pool, tenant_id: uuid.UUID, counts: Counter[str], *, nudged: bool
    ) -> str | None:
        """``sync.proposal_sets`` when :meth:`due` (else counted ``proposal_check_skipped``); remembers a clean check
        (nothing due, no outage, no error). Returns the step's outage class."""
        if not self.due(tenant_id, nudged=nudged):
            counts["proposal_check_skipped"] += 1
            return None
        self.clean_at.pop(tenant_id, None)
        before, started = work_done(counts), self.clock()
        outage: str | None = await sync.proposal_sets(pool, tenant_id, counts)
        if outage is None and work_done(counts) == before:
            self.clean_at[tenant_id] = started
        return outage

    def forget(self) -> None:
        """Every agency is checked on the next minute run (tests)."""
        self.clean_at.clear()


CHECKS = ProposalChecks()  # the process's memory, used by app.booking.sync.BookingSync.run_agency
