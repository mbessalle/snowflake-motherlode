"""Traces and logs for every step, with the inputs and outputs of each decision. OpenTelemetry throughout.

    from motherlode import telemetry
    telemetry.setup("score_pdf")                       # once, in a script; not inside Snowflake
    with telemetry.span("score_case", case_id=..., input_reply=reply) as s:
        s.set(output_agree=True)

Where the spans go:
  local runs   traces/<service>-<time>.spans.jsonl, one JSON object per finished span (for code to query:
               scripts/traces.py), and an OTLP endpoint such as a local Jaeger when OTEL_EXPORTER_OTLP_ENDPOINT
               is set (http://127.0.0.1:4318). Logs go to traces/<service>-<time>.logs.jsonl with the trace
               and span id of the step that wrote them, and are also added to that span as events.
  Snowflake    a stored procedure calls span() without setup(): Snowflake's own OpenTelemetry provider sends
               the spans to the account's event table (TRACE_LEVEL must be ON_EVENT or ALWAYS).
  neither      without the opentelemetry package span() still works and records nothing.

Attribute values that are not str, bool, int or float (dicts, lists, None) are stored as JSON text, and long
text is cut at MAX_TEXT characters, so a span can carry a whole extraction reply as its input.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import json
import logging
import os
from pathlib import Path

try:
    from opentelemetry import trace
except ImportError:          # no OpenTelemetry: spans are no-ops
    trace = None

MAX_TEXT = 4000
TRACES = Path(__file__).resolve().parent.parent / "traces"
log = logging.getLogger("motherlode")
_configured: dict = {}      # the files of this process's run, once setup() has run


def _value(v):
    if isinstance(v, bool) or isinstance(v, float):
        return v
    if isinstance(v, int):
        # OTLP stores integers as int64; a larger one was dropped by the exporter (critic, round 2)
        return v if -2**63 <= v < 2**63 else str(v)
    text = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False, default=str)
    return text if len(text) <= MAX_TEXT else text[:MAX_TEXT] + f"... [{len(text)} chars]"


class Span:
    """A thin wrapper so callers write span.set(name=value) with any value type."""

    def __init__(self, inner=None):
        self.inner = inner

    def set(self, **attributes) -> Span:
        if self.inner is not None:
            for key, value in attributes.items():
                self.inner.set_attribute(key, _value(value))
        return self

    def event(self, name: str, **attributes) -> None:
        if self.inner is not None:
            self.inner.add_event(name, {k: _value(v) for k, v in attributes.items()})

    @property
    def ids(self) -> tuple[str, str] | None:
        if self.inner is None:
            return None
        context = self.inner.get_span_context()
        if not context.is_valid:        # outside any span: no ids, not zeros that match no span (critic, round 2)
            return None
        return format(context.trace_id, "032x"), format(context.span_id, "016x")


@contextlib.contextmanager
def span(name: str, **attributes):
    if trace is None:
        yield Span()
        return
    with trace.get_tracer("motherlode").start_as_current_span(name) as inner:
        yield Span(inner).set(**attributes)


def carry(fn):
    """fn wrapped to run in the caller's trace context, for a thread pool: spans made in a worker thread
    then nest under the span that submitted the work instead of starting a trace of their own."""
    if trace is None:
        return fn
    from opentelemetry import context

    parent = context.get_current()

    def run(*args, **kwargs):
        token = context.attach(parent)
        try:
            return fn(*args, **kwargs)
        finally:
            context.detach(token)
    return run


def current() -> Span:
    return Span(trace.get_current_span()) if trace is not None else Span()


# --- local setup ------------------------------------------------------------------------------------------

class _JsonlSpanExporter:
    """Writes each finished span as one JSON line: name, ids, parent, times, status, attributes, events."""

    def __init__(self, path: Path):
        self.path = path
        self.file = path.open("a")

    def export(self, spans):
        from opentelemetry.sdk.trace.export import SpanExportResult

        for s in spans:
            self.file.write(json.dumps({
                "name": s.name, "trace_id": format(s.context.trace_id, "032x"), "span_id": format(s.context.span_id, "016x"),
                "parent_id": format(s.parent.span_id, "016x") if s.parent else None,
                "start": s.start_time, "end": s.end_time, "duration_ms": (s.end_time - s.start_time) / 1e6,
                "status": s.status.status_code.name, "status_description": s.status.description,
                "attributes": dict(s.attributes),
                "events": [{"name": e.name, "attributes": dict(e.attributes)} for e in s.events],
            }, ensure_ascii=False, default=str) + "\n")
        self.file.flush()
        return SpanExportResult.SUCCESS

    def shutdown(self):
        self.file.close()

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        self.file.flush()
        return True


class _JsonlLogHandler(logging.Handler):
    """Each log record as one JSON line with the trace and span id of the step that wrote it, and as an
    event on that span so the log line shows up in the trace viewer next to the step."""

    def __init__(self, path: Path):
        super().__init__()
        self.file = path.open("a")

    def emit(self, record):
        ids = current().ids
        entry = {"time": dt.datetime.fromtimestamp(record.created, dt.UTC).isoformat(), "level": record.levelname,
                 "logger": record.name, "message": record.getMessage(),
                 "trace_id": ids[0] if ids else None, "span_id": ids[1] if ids else None}
        self.file.write(json.dumps(entry, ensure_ascii=False) + "\n")
        self.file.flush()
        current().event("log", level=record.levelname, message=record.getMessage())


def setup(service: str, directory: Path = TRACES) -> dict:
    """Configure tracing and logging for a local run. Returns the paths written and the trace endpoint.
    Does nothing (and returns {}) without the OpenTelemetry SDK."""
    global _configured
    try:
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor
    except ImportError:
        return {}
    if _configured:
        # OpenTelemetry allows one provider per process: a second setup would create files that never
        # receive a span, so it returns the first run's files instead (critic finding 12)
        log.warning("telemetry.setup(%r) called again; spans keep going to %s", service, _configured["spans"])
        return _configured
    directory.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    spans_path, logs_path = directory / f"{service}-{stamp}.spans.jsonl", directory / f"{service}-{stamp}.logs.jsonl"
    provider = TracerProvider(resource=Resource.create({"service.name": f"motherlode.{service}"}))
    provider.add_span_processor(SimpleSpanProcessor(_JsonlSpanExporter(spans_path)))
    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
    if endpoint:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint.rstrip("/") + "/v1/traces")))
    trace.set_tracer_provider(provider)
    log.setLevel(logging.INFO)
    log.addHandler(_JsonlLogHandler(logs_path))
    _configured = {"spans": str(spans_path), "logs": str(logs_path), "otlp": endpoint}
    return _configured


def shutdown() -> None:
    """Flush every exporter; call at the end of a script so the last spans are written."""
    if trace is not None:
        provider = trace.get_tracer_provider()
        if hasattr(provider, "shutdown"):
            provider.shutdown()
