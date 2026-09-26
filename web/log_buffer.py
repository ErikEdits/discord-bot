"""In-memory ring buffer for log records so the panel can show recent logs."""

import logging
from collections import deque
from typing import Deque


LOG_BUFFER: Deque[dict] = deque(maxlen=500)


class BufferHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            LOG_BUFFER.append({
                "time": record.created,
                "level": record.levelname,
                "name": record.name,
                "message": self.format(record),
            })
        except Exception:
            pass


def install() -> None:
    """Attach the buffer handler to the root logger (idempotent)."""
    root = logging.getLogger()
    if any(isinstance(h, BufferHandler) for h in root.handlers):
        return
    handler = BufferHandler()
    handler.setLevel(logging.INFO)
    handler.setFormatter(logging.Formatter("%(message)s"))
    root.addHandler(handler)
