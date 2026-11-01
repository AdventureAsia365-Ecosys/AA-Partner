"""Mail counters the job sampler logs (AA-945): ``mail_dropped`` on its ``ops_metrics_sampled`` line (until S263 the
CloudWatch metric ``MailIngestDropped``; it never had an alarm). Per process, reset on each sample."""

from __future__ import annotations

import threading


class DropCounter:
    """Mail discarded for an unknown address since the last sample (``mail_dropped``)."""

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
