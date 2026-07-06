"""Tests for OpenTelemetry instrumentation (Stage 3.4).

Uses a real TracerProvider wired to an in-memory span exporter (not mocks)
so assertions check actual span names/attributes/status as OTel itself
would report them, rather than asserting on our own wrapper's internals.
"""

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from app.observability import record_llm_call, record_tool_call, trace_graph_node


# OTel's global API only allows trace.set_tracer_provider() to succeed
# once per process — a second call is silently ignored (with a warning),
# so the in-memory provider/exporter must be installed exactly once for
# this whole test module rather than per-test. Each test clears the
# exporter's buffer instead of swapping providers, to stay isolated.
_EXPORTER = InMemorySpanExporter()
_PROVIDER = TracerProvider()
_PROVIDER.add_span_processor(SimpleSpanProcessor(_EXPORTER))
trace.set_tracer_provider(_PROVIDER)


@pytest.fixture
def span_exporter():
    _EXPORTER.clear()
    yield _EXPORTER
    _EXPORTER.clear()


async def test_trace_graph_node_wraps_async_node_and_records_success(span_exporter):
    @trace_graph_node("my_node")
    async def some_node(state):
        return {"done": True}

    result = await some_node({"x": 1})

    assert result == {"done": True}
    spans = span_exporter.get_finished_spans()
    assert len(spans) == 1
    assert spans[0].name == "agent.node.my_node"
    assert spans[0].status.status_code == StatusCode.OK
    assert spans[0].attributes["duration_ms"] >= 0


def test_trace_graph_node_wraps_sync_node_and_records_success(span_exporter):
    @trace_graph_node("sync_node")
    def some_node(state):
        return {"done": True}

    result = some_node({"x": 1})

    assert result == {"done": True}
    spans = span_exporter.get_finished_spans()
    assert len(spans) == 1
    assert spans[0].name == "agent.node.sync_node"
    assert spans[0].status.status_code == StatusCode.OK


async def test_trace_graph_node_records_error_status_and_reraises(span_exporter):
    @trace_graph_node("failing_node")
    async def some_node(state):
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        await some_node({})

    spans = span_exporter.get_finished_spans()
    assert len(spans) == 1
    assert spans[0].status.status_code == StatusCode.ERROR
    assert spans[0].events[0].name == "exception"


def test_trace_graph_node_preserves_function_identity():
    @trace_graph_node("named_node")
    async def some_node(state):
        """docstring"""
        return state

    assert some_node.__name__ == "some_node"


async def test_record_llm_call_sets_gen_ai_attributes_on_current_span(span_exporter):
    tracer = trace.get_tracer("test")

    class _FakeResponse:
        usage_metadata = {"input_tokens": 10, "output_tokens": 5}

    with tracer.start_as_current_span("test-span"):
        record_llm_call("plan", "llama-3.1-8b-instant", _FakeResponse())

    spans = span_exporter.get_finished_spans()
    assert spans[0].attributes["gen_ai.request.model"] == "llama-3.1-8b-instant"
    assert spans[0].attributes["gen_ai.usage.input_tokens"] == 10
    assert spans[0].attributes["gen_ai.usage.output_tokens"] == 5


async def test_record_llm_call_tolerates_missing_usage_metadata(span_exporter):
    tracer = trace.get_tracer("test")

    class _FakeResponse:
        pass

    with tracer.start_as_current_span("test-span"):
        record_llm_call("plan", "llama-3.1-8b-instant", _FakeResponse())

    spans = span_exporter.get_finished_spans()
    assert spans[0].attributes["gen_ai.request.model"] == "llama-3.1-8b-instant"
    assert "gen_ai.usage.input_tokens" not in spans[0].attributes


async def test_record_tool_call_sets_mcp_attributes_on_success(span_exporter):
    tracer = trace.get_tracer("test")

    with tracer.start_as_current_span("test-span"):
        record_tool_call("identity_get_user", ok=True, domain="identity")

    spans = span_exporter.get_finished_spans()
    assert spans[0].attributes["mcp.tool.name"] == "identity_get_user"
    assert spans[0].attributes["mcp.tool.success"] is True
    assert spans[0].attributes["mcp.tool.domain"] == "identity"


async def test_record_tool_call_omits_domain_when_not_given(span_exporter):
    tracer = trace.get_tracer("test")

    with tracer.start_as_current_span("test-span"):
        record_tool_call("some_tool", ok=False)

    spans = span_exporter.get_finished_spans()
    assert spans[0].attributes["mcp.tool.success"] is False
    assert "mcp.tool.domain" not in spans[0].attributes


async def test_configure_observability_is_idempotent(monkeypatch):
    """Calling configure_observability() twice must not raise or double-set
    the global tracer provider — main.py calls it at import time, and
    pytest importing app.api.main more than once (or other modules doing
    the same) must stay a safe no-op past the first call.
    """
    import app.observability as observability

    monkeypatch.setattr(observability, "_configured", False)
    observability.configure_observability()
    observability.configure_observability()


async def test_no_op_when_otel_endpoint_unset(monkeypatch):
    """With OTEL_EXPORTER_OTLP_ENDPOINT unset (the default/local-dev case),
    configure_observability() must return before ever calling
    trace.set_tracer_provider() — tracing stays a harmless no-op rather
    than crashing or trying to export anywhere. Asserted via a spy rather
    than "provider didn't change", since OTel's global API silently
    ignores a second set_tracer_provider() call regardless of whether this
    function makes it — that would pass even if the early-return broke.
    """
    import app.observability as observability
    from app.config import get_settings

    monkeypatch.setattr(observability, "_configured", False)
    settings = get_settings()
    monkeypatch.setattr(settings, "otel_exporter_endpoint", "")
    monkeypatch.setattr(observability, "get_settings", lambda: settings)

    called = False
    original_set = trace.set_tracer_provider

    def _spy(provider):
        nonlocal called
        called = True
        original_set(provider)

    monkeypatch.setattr(trace, "set_tracer_provider", _spy)
    observability.configure_observability()
    assert called is False
