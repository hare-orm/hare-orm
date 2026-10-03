import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import INVALID_SPAN

from hare.contrib.opentelemetry import OpenTelemetryInstrumentor
from hare.contrib.test import requires_features
from hare.instrumentation.observers import Observers
from hare.instrumentation.query_executed import QueryExecuted
from hare.transactions.transactions import Transactions


@pytest.fixture
def exporter():
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    yield exporter, provider
    exporter.shutdown()


@pytest.mark.asyncio
async def test_instrument_records_a_span_with_db_attributes(db, exporter):
    span_exporter, provider = exporter
    instrumentor = OpenTelemetryInstrumentor(tracer_provider=provider)
    instrumentor.instrument()
    try:
        await db.db().execute("SELECT 1")
    finally:
        instrumentor.uninstrument()

    spans = span_exporter.get_finished_spans()
    assert spans, "no span was recorded"
    query_span = spans[-1]
    assert query_span.name == "execute"
    assert query_span.attributes["db.system.name"] == db.db().dialect.otel_system_name
    assert query_span.attributes["db.query.text"] == "SELECT 1"


@pytest.mark.asyncio
@requires_features(supports_streaming=True)
async def test_instrument_records_a_span_for_a_stream_query(db, exporter):
    """stream() (QuerySet.stream()'s own execution primitive) is an async
    generator, not a plain coroutine - it wasn't in QUERY_EXECUTING_METHOD_NAMES at all, so
    instrument() never wrapped it, and a .stream() query executed completely outside any span
    even with instrumentation active."""
    from tests.testmodels import Author

    await Author.objects.create(name="Some Author")

    span_exporter, provider = exporter
    instrumentor = OpenTelemetryInstrumentor(tracer_provider=provider)
    instrumentor.instrument()
    try:
        async with Transactions.atomic():
            async for _ in Author.objects.all().stream():
                pass
    finally:
        instrumentor.uninstrument()

    spans = [span for span in span_exporter.get_finished_spans() if span.name == "stream"]
    assert spans, "no span was recorded for the streamed query"
    assert spans[0].attributes["db.system.name"] == db.db().dialect.otel_system_name
    assert "SELECT" in str(spans[0].attributes["db.query.text"]).upper()


@pytest.mark.asyncio
@requires_features(supports_streaming=True)
async def test_stream_span_does_not_stay_current_after_the_caller_breaks_early(db, exporter):
    """A `with start_as_current_span(...): async for item in original(...): yield item` shape held
    the stream span "current" across every yield, including while suspended waiting for the caller
    to pull the next row. A caller breaking out of `.stream()` before exhausting it never resumes
    that generator, so the `with` block never exits - the span stayed "current" indefinitely, and
    an unrelated span opened afterward incorrectly nested underneath it."""
    from tests.testmodels import Author

    await Author.objects.create(name="A")
    await Author.objects.create(name="B")

    span_exporter, provider = exporter
    tracer = provider.get_tracer("test")
    instrumentor = OpenTelemetryInstrumentor(tracer_provider=provider)
    instrumentor.instrument()
    try:
        async with Transactions.atomic():
            async for _ in Author.objects.all().stream():
                break

        with tracer.start_as_current_span("unrelated_followup") as followup:
            followup_parent = followup.parent
    finally:
        instrumentor.uninstrument()

    assert followup_parent is None, (
        "an unrelated span opened after breaking out of stream() early incorrectly nested under "
        "the still-current, unclosed stream span"
    )


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_instrument_records_a_span_for_a_copy_bulk_create(db_truncate, exporter):
    """bulk_create(use_copy=True) ran through copy(), which was never wrapped, so the
    COPY load executed outside any span. Not transaction-wrapped: rust_pg's COPY can't run
    inside a transaction."""
    from tests.testmodels import Tournament

    span_exporter, provider = exporter
    instrumentor = OpenTelemetryInstrumentor(tracer_provider=provider)
    instrumentor.instrument()
    try:
        await Tournament.objects.bulk_create([Tournament(id=1, name="A"), Tournament(id=2, name="B")], use_copy=True)
    finally:
        instrumentor.uninstrument()

    spans = [span for span in span_exporter.get_finished_spans() if span.name == "copy"]
    assert len(spans) == 1
    statement = str(spans[0].attributes["db.query.text"])
    assert statement.startswith("COPY tournament (")
    assert "name" in statement
    assert "A" not in statement.removeprefix("COPY tournament")
    assert spans[0].attributes["db.system.name"] == Tournament._meta.db.dialect.otel_system_name


@pytest.mark.asyncio
async def test_uninstrument_stops_recording_spans(db, exporter):
    span_exporter, provider = exporter
    instrumentor = OpenTelemetryInstrumentor(tracer_provider=provider)
    instrumentor.instrument()
    instrumentor.uninstrument()

    await db.db().execute("SELECT 1")

    assert span_exporter.get_finished_spans() == ()


@pytest.mark.asyncio
async def test_instrument_is_idempotent(db, exporter):
    """A second instrument() call on an already-instrumented instance must not double-wrap -
    otherwise a single query would produce two nested spans instead of one."""
    span_exporter, provider = exporter
    instrumentor = OpenTelemetryInstrumentor(tracer_provider=provider)
    instrumentor.instrument()
    instrumentor.instrument()
    try:
        await db.db().execute("SELECT 1")
    finally:
        instrumentor.uninstrument()

    spans = span_exporter.get_finished_spans()
    assert len(spans) == 1


@pytest.mark.asyncio
async def test_record_sql_statement_false_omits_the_statement_attribute(db, exporter):
    span_exporter, provider = exporter
    instrumentor = OpenTelemetryInstrumentor(tracer_provider=provider, record_sql_statement=False)
    instrumentor.instrument()
    try:
        await db.db().execute("SELECT 1")
    finally:
        instrumentor.uninstrument()

    query_span = span_exporter.get_finished_spans()[-1]
    assert "db.query.text" not in query_span.attributes
    assert query_span.attributes["db.system.name"] == db.db().dialect.otel_system_name


@pytest.mark.asyncio
async def test_a_failed_query_records_the_exception_and_error_status(db, exporter):
    span_exporter, provider = exporter
    instrumentor = OpenTelemetryInstrumentor(tracer_provider=provider)
    instrumentor.instrument()
    try:
        from hare.exceptions import OperationalError

        with pytest.raises(OperationalError):
            await db.db().execute("SELECT * FROM this_table_does_not_exist")
    finally:
        instrumentor.uninstrument()

    query_span = span_exporter.get_finished_spans()[-1]
    assert query_span.status.status_code == trace.StatusCode.ERROR
    assert len(query_span.events) == 1
    assert query_span.events[0].name == "exception"


@pytest.mark.asyncio
async def test_a_failed_query_exception_event_carries_no_bind_parameter_values(db, exporter):
    from hare.exceptions import IntegrityError
    from tests.testmodels import Tournament

    span_exporter, provider = exporter
    secret_value = "$argon2id$v=19$SECRET-HASH-VALUE"
    await Tournament.objects.create(id=1, name="first")
    instrumentor = OpenTelemetryInstrumentor(tracer_provider=provider)
    instrumentor.instrument()
    try:
        with pytest.raises(IntegrityError) as exception_info:
            await Tournament.objects.create(id=1, name=secret_value)
    finally:
        instrumentor.uninstrument()

    assert secret_value in exception_info.value.params
    exception_events = [
        event for span in span_exporter.get_finished_spans() for event in span.events if event.name == "exception"
    ]
    assert exception_events
    for event in exception_events:
        assert secret_value not in str(dict(event.attributes))


@pytest.mark.asyncio
async def test_query_span_is_correctly_nested_under_a_manually_opened_outer_span(db, exporter):
    """The whole point of wrapping the driver call directly instead of going through
    hare.instrumentation's post-hoc hooks: the span must reflect real-time parent/child
    nesting, exactly like a hand-written `with tracer.start_as_current_span(...)` around the
    query would."""
    span_exporter, provider = exporter
    tracer = provider.get_tracer(__name__)
    instrumentor = OpenTelemetryInstrumentor(tracer_provider=provider)
    instrumentor.instrument()
    try:
        with tracer.start_as_current_span("outer_operation") as outer_span:
            await db.db().execute("SELECT 1")
    finally:
        instrumentor.uninstrument()

    spans = span_exporter.get_finished_spans()
    query_span = next(span for span in spans if span.name == "execute")
    outer = next(span for span in spans if span.name == "outer_operation")
    assert query_span.parent.span_id == outer_span.get_span_context().span_id
    assert outer.context.span_id == outer_span.get_span_context().span_id


@pytest.mark.asyncio
async def test_a_registered_query_hook_sees_the_same_span_via_get_current_span(db, exporter):
    """Correlation between this module and hare.instrumentation's ordinary query hooks is not
    wired explicitly anywhere - it works because the span opened here stays current for the
    whole duration of the wrapped call, including the moment QueryInstrumentation.record() (and
    any hook it fires) runs. A hook that calls opentelemetry.trace.get_current_span() must see
    exactly the span this instrumentor created for the query it's observing."""
    span_exporter, provider = exporter
    instrumentor = OpenTelemetryInstrumentor(tracer_provider=provider)

    seen_span_ids = []

    def hook(event):
        seen_span_ids.append(trace.get_current_span().get_span_context().span_id)

    Observers.observe(QueryExecuted, hook)
    instrumentor.instrument()
    try:
        await db.db().execute("SELECT 1")
        await Observers.wait_for_pending()
    finally:
        instrumentor.uninstrument()
        Observers.unobserve(QueryExecuted, hook)

    query_span = span_exporter.get_finished_spans()[-1]
    assert seen_span_ids, "the hook never ran - the correlation check below would be vacuous"
    assert seen_span_ids[0] != INVALID_SPAN.get_span_context().span_id
    assert seen_span_ids[0] == query_span.get_span_context().span_id


@pytest.mark.asyncio
async def test_two_instrumentors_each_record_their_own_span(db, exporter):
    """Instrumentors are query wrappers of their own: a second one adds a span nested in the
    first one's, and removing either leaves the other working."""
    span_exporter, provider = exporter
    outer_instrumentor = OpenTelemetryInstrumentor(tracer_provider=provider)
    inner_instrumentor = OpenTelemetryInstrumentor(tracer_provider=provider)
    outer_instrumentor.instrument()
    inner_instrumentor.instrument()
    try:
        await db.db().execute("SELECT 1")
        spans = [span for span in span_exporter.get_finished_spans() if span.name == "execute"]
        assert len(spans) == 2
        inner_span, outer_span = spans
        assert inner_span.parent.span_id == outer_span.get_span_context().span_id

        outer_instrumentor.uninstrument()
        span_exporter.clear()
        await db.db().execute("SELECT 1")
        assert [span.name for span in span_exporter.get_finished_spans()] == ["execute"]
    finally:
        outer_instrumentor.uninstrument()
        inner_instrumentor.uninstrument()
