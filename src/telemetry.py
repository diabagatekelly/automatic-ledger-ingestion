"""Structured-log emitter shared by every telemetry event (#34, #44).

One JSON object per line, printed to stdout: Cloud Run's logging agent parses it
into ``jsonPayload`` with queryable fields, so Logs Explorer filters and
log-based metrics work with **no logging library and no external service**.

Each event family (``gemini_parse``, ``media_download``, …) owns its field
vocabulary next to its domain code (``src/llm.py``, ``src/media.py``); this
module owns only the line format, so the schema the metrics count can't drift
between emitters. See ``docs/OBSERVABILITY.md`` for the schemas and metrics.
"""

from __future__ import annotations

import json


def log_event(event: str, outcome: str, *, severity: str, **fields: str | None) -> None:
    """Emit one structured JSON log line for a telemetry event.

    ``None``-valued fields are omitted entirely (absent beats empty for label
    extraction). ``message`` is a human-readable summary of the same fields, so
    the plain log stream stays legible without expanding each entry.
    """
    present = {key: value for key, value in fields.items() if value is not None}
    detail = "".join(f" {key}={value}" for key, value in present.items())
    entry: dict[str, str] = {
        "severity": severity,
        "message": f"{event} outcome={outcome}{detail}",
        "event": event,
        "outcome": outcome,
        **present,
    }
    print(json.dumps(entry), flush=True)
