"""Structured logs. Inputs and replies are not logged: they can hold personal data."""

from __future__ import annotations

import json
import logging
from typing import Any

log = logging.getLogger("agentstudio")


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        data = {"ts": round(record.created, 3), "level": record.levelname, "msg": record.getMessage()}
        data.update(getattr(record, "fields", {}))
        return json.dumps(data, default=str)


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    log.handlers[:] = [handler]
    log.setLevel(level.upper())
    log.propagate = False


def event(name: str, **fields: Any) -> None:
    log.info(name, extra={"fields": {"event": name, **fields}})
