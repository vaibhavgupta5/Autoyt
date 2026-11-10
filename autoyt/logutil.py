"""Logging setup plus a small ring buffer of recent lines for failure emails."""
from __future__ import annotations

import logging
import sys
from collections import deque

_RING: deque[str] = deque(maxlen=60)


class _Ring(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            _RING.append(self.format(record))
        except Exception:  # noqa: BLE001
            pass


def recent_log(n: int = 25) -> str:
    return "\n".join(list(_RING)[-n:])


def setup_logging(level: int = logging.INFO) -> None:
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    root = logging.getLogger()
    if any(isinstance(h, _Ring) for h in root.handlers):
        return
    root.setLevel(level)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    ring = _Ring()
    ring.setFormatter(fmt)
    root.addHandler(sh)
    root.addHandler(ring)
