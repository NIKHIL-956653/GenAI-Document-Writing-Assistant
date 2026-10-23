"""
Structured logging.

Modules log a short event message plus key/value context through `extra=`:

    log.info("ticket fetched", extra={"ticket": key, "title": title})

`text` format (default, for humans) renders that as

    12:04:31 INFO  docgen.jira  ticket fetched  ticket=DEMO-2 title='Add login'

and `json` format emits one JSON object per line for log shippers. Logs go
to stderr so stdout stays clean for document output.
"""
from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone

# Attributes every LogRecord has; anything else on the record came from `extra=`.
_RESERVED = set(vars(logging.LogRecord("", 0, "", 0, "", None, None))) | {"message", "asctime"}


def _context(record: logging.LogRecord) -> dict:
    return {k: v for k, v in vars(record).items() if k not in _RESERVED and not k.startswith("_")}


class TextFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        ts = datetime.fromtimestamp(record.created).strftime("%H:%M:%S")
        line = f"{ts} {record.levelname:<5} {record.name:<20} {record.getMessage()}"
        ctx = _context(record)
        if ctx:
            line += "  " + " ".join(f"{k}={_fmt(v)}" for k, v in ctx.items())
        if record.exc_info:
            line += "\n" + self.formatException(record.exc_info)
        return line


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname.lower(),
            "logger": record.name,
            "event": record.getMessage(),
            **_context(record),
        }
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def _fmt(value) -> str:
    if isinstance(value, float):
        return f"{value:.6g}"
    text = str(value)
    return repr(text) if (" " in text or not text) else text


def configure(level: str = "INFO", fmt: str = "text") -> None:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFormatter() if fmt == "json" else TextFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(logging.WARNING)
    # Our own loggers at the requested level; third-party libraries stay quiet.
    logging.getLogger("docgen").setLevel(level.upper())
