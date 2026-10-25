"""Mail counters the job metrics sampler publishes (AA-945): ``MailIngestDropped`` in ``AA/Jobs/Crm`` (the only
namespace the task role may write), so no new IAM grant is needed. Per process, reset on each sample."""

from __future__ import annotations

import threading


class DropCounter:
    """Mail discarded for an unknown address since the last metrics sample (``MailIngestDropped``, AA/Jobs/Crm)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._count = 0

    def add(self, n: int = 1) -> None:
        with self._lock:
            self._count += n

    def take(self) -> int:
        with self._lock:
            value, self._count = self._count, 0
            return value


DROPPED = DropCounter()
