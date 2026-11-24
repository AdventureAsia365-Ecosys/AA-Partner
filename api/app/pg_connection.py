"""The asyncpg connection class of every CRM pool: savepoint names that repeat (S291, C4.3 W48 finding 5, AA-838).

asyncpg names each nested ``conn.transaction()`` savepoint after a process-wide counter
(``__asyncpg_savepoint_1f3a__``), so every ``SAVEPOINT`` / ``RELEASE SAVEPOINT`` / ``ROLLBACK TO`` the CRM sends has a
text never seen before. ``pg_stat_statements`` keys such a utility statement by its text (``track_utility`` on; the
shared RDS runs PostgreSQL 15, and a local PostgreSQL 16 does the same: 200 nested transactions made 400 entries), so
each ``tenant_tx()`` / ``system_tx()`` (one savepoint for the route's or the job's work) added two entries of one call
each. That is the likely reason ``crm_app`` held about 90 % of the 10,000 tracked statements and the evictions reset
the counters the weekly C4.3 review compares.

Here a savepoint is named after its depth on the connection instead: ``crm_sp_1`` for the first savepoint inside the
transaction, ``crm_sp_2`` inside that one, and so on, so the texts are a handful for the life of the database.

Why depth and not one fixed name: ``ROLLBACK TO`` keeps the savepoint it rolled back to. With one name for all, an
outer block failing after an inner block was rolled back (and caught) would roll back only to the inner savepoint. The
savepoint open at depth ``d`` is always the newest one named ``crm_sp_<d>`` (a new ``crm_sp_<d>`` can only start once
the previous one at that depth has ended), so ``RELEASE`` and ``ROLLBACK TO`` by name reach exactly the block asyncpg
means. The depth is the number of this connection's savepoint blocks still open (state ``STARTED``); a new top-level
transaction starts from zero.

Used by :class:`app.db.Database` (the API and worker pool) and the restore drill's pool
(:mod:`app.portability.restore_drill`). Relies on two asyncpg internals, both pinned by ``requirements.txt`` and
checked by ``tests/unit/test_s291_pg_connection.py``: ``Transaction.start`` takes the savepoint name from
``Connection._get_unique_id("savepoint")`` and stores it in ``Transaction._id``.
"""

from __future__ import annotations

from typing import Any

from asyncpg import Connection
from asyncpg.transaction import Transaction, TransactionState

SAVEPOINT_PREFIX = "crm_sp_"


class StableSavepointTransaction(Transaction):
    """A transaction block whose savepoint (when nested) is named after its depth (:data:`SAVEPOINT_PREFIX` + depth)."""

    __slots__ = ()

    async def start(self) -> None:
        con: Any = self._connection
        if con._top_xact is None:
            con._crm_savepoints = []  # a new top-level transaction: no savepoint of an older one is open
        await super().start()
        if self._nested and self._state is TransactionState.STARTED:
            con._crm_savepoints.append(self)


class StableSavepointConnection(Connection):  # type: ignore[misc]
    """``connection_class`` of the CRM pools: ``transaction()`` returns a :class:`StableSavepointTransaction`."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._crm_savepoints: list[Transaction] = []

    def transaction(
        self, *, isolation: str | None = None, readonly: bool = False, deferrable: bool = False
    ) -> StableSavepointTransaction:
        self._check_open()
        return StableSavepointTransaction(self, isolation, readonly, deferrable)

    def _get_unique_id(self, prefix: str) -> str:
        if prefix != "savepoint":
            return str(super()._get_unique_id(prefix))  # prepared statement and cursor names: protocol level only
        self._crm_savepoints = [t for t in self._crm_savepoints if t._state is TransactionState.STARTED]
        return savepoint_name(len(self._crm_savepoints) + 1)


def savepoint_name(depth: int) -> str:
    """The savepoint of a block nested ``depth`` levels inside the top-level transaction (1 = the first)."""
    return f"{SAVEPOINT_PREFIX}{depth}"
