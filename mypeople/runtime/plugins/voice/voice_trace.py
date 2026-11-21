"""Minimal, best-effort OTLP/HTTP tracing for the stdlib-only voice bridge."""

from __future__ import annotations

import http.client
import json
import os
import secrets
import time
import urllib.request

ENDPOINT = os.environ.get("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "http://127.0.0.1:14318/v1/traces")
SERVICE = "myplow-voice-plugin"


def _attribute(key: str, value: object) -> dict:
    if isinstance(value, bool):
        item = {"boolValue": value}
    elif isinstance(value, int):
        item = {"intValue": str(value)}
    elif isinstance(value, float):
        item = {"doubleValue": value}
    else:
        item = {"stringValue": str(value)}
    return {"key": key, "value": item}


class Span:
    def __init__(self, name: str, parent: str = "", attributes: dict | None = None):
        self.name = name
        self.trace_id = secrets.token_hex(16)
        self.parent_id = ""
        if len(parent.split("-")) == 4:
            version, trace_id, parent_id, _flags = parent.split("-")
            if version == "00" and len(trace_id) == 32 and len(parent_id) == 16:
                try:
                    int(trace_id, 16)
                    int(parent_id, 16)
                    self.trace_id, self.parent_id = trace_id.lower(), parent_id.lower()
                except ValueError:
                    pass
        self.span_id = secrets.token_hex(8)
        self.start_ns = time.time_ns()
        self.end_ns = self.start_ns
        self.attributes = dict(attributes or {})
        self.error = False

    @property
    def traceparent(self) -> str:
        return f"00-{self.trace_id}-{self.span_id}-01"

    def set_attribute(self, key: str, value: object) -> None:
        self.attributes[key] = value

    def __enter__(self):
        return self

    def __exit__(self, exc_type, _exc, _tb) -> None:
        self.end_ns = time.time_ns()
        self.error = exc_type is not None
        span = {
            "traceId": self.trace_id,
            "spanId": self.span_id,
            "name": self.name,
            "kind": 1,
            "startTimeUnixNano": str(self.start_ns),
            "endTimeUnixNano": str(self.end_ns),
            "attributes": [_attribute(k, v) for k, v in self.attributes.items()],
            "status": {"code": 2 if self.error else 1},
        }
        if self.parent_id:
            span["parentSpanId"] = self.parent_id
        payload = {
            "resourceSpans": [
                {
                    "resource": {"attributes": [_attribute("service.name", SERVICE)]},
                    "scopeSpans": [{"scope": {"name": "myplow.voice"}, "spans": [span]}],
                }
            ]
        }
        try:
            request = urllib.request.Request(
                ENDPOINT,
                data=json.dumps(payload).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=0.25) as response:
                response.read()
        except (OSError, ValueError, http.client.HTTPException):
            return


def start_span(name: str, *, parent: str = "", attributes: dict | None = None) -> Span:
    return Span(name, parent=parent, attributes=attributes)
