# OpenTelemetry

Opens a real OpenTelemetry span around every query hare-orm's driver clients run, correctly
nested under whatever span is already active — the same shape a hand-written
`with tracer.start_as_current_span(...)` around the query would give — and reports the metrics of
every pool of connections ([Pool metrics and health](pool-health.md#opentelemetry-metrics)).

```bash
pip install hare-orm[opentelemetry]
```

```python
from hare.contrib.opentelemetry import OpenTelemetryInstrumentor

instrumentor = OpenTelemetryInstrumentor()
instrumentor.instrument()
...
instrumentor.uninstrument()
```

## <a id="why-not-query-hooks"></a>A query wrapper, not an observer

hare-orm reports every finished query to its [observers](observers.md) as a `QueryExecuted`
event. A span can't be built from that: the event arrives *after* the query has completed, so
nothing about it gives the span correct parent/child nesting relative to the other spans open at
the time. The instrumentor is a [`QueryWrapper`](observers.md#query-wrappers) instead — the
mechanism hare has for running code *around* a query: `instrument()` installs it with
`Observers.wrap_queries()`, and it opens the span immediately before the driver call and closes it
immediately after, in the task that issued the query — exactly how
`opentelemetry-instrumentation-dbapi`/`-sqlalchemy` and similar instrumentation packages work.
Nothing is patched on the driver clients: every dialect and driver, third-party ones included, gets
spans through the same wrapper.

**Correlation with an observer comes for free.** The span stays current for the whole wrapped call,
including the moment a plain-function observer of `QueryExecuted` is called from inside it, so an
observer that calls `opentelemetry.trace.get_current_span()` sees exactly the span opened for the
query it's observing — with no coupling between the two.

## <a id="opentelemetryinstrumentor"></a>`OpenTelemetryInstrumentor`

```python
class OpenTelemetryInstrumentor:
    def __init__(
        self,
        tracer_provider: TracerProvider | None = None,
        meter_provider: MeterProvider | None = None,
        *,
        record_sql_statement: bool = True,
        query_spans: bool = True,
        pool_metrics: bool = True,
    ) -> None: ...
    def instrument(self) -> None: ...
    def uninstrument(self) -> None: ...
```

- `meter_provider`: the MeterProvider of the pool metrics — the global one
  (`opentelemetry.metrics.get_meter_provider()`) when not passed.
- `query_spans`/`pool_metrics`: whether `instrument()` starts the spans (`QuerySpans`) and the pool
  metrics (`PoolMetricInstruments`). The pool metrics also enable `PoolMetrics` — the measuring of
  each wait for a connection — while instrumented; the metrics are listed in
  [Pool metrics and health](pool-health.md#opentelemetry-metrics).

- `tracer_provider`: defaults to the global one (`opentelemetry.trace.get_tracer_provider()`) if
  you don't pass one — set it up (an SDK `TracerProvider` with your own exporter) before calling
  `instrument()`.
- `record_sql_statement`: when `True` (the default), sets the `db.query.text` span attribute to the
  already parameter-bound SQL text (hare-orm never inlines literal values into it, so this is safe
  by construction). Set `False` to omit it entirely if even the query *structure* is sensitive.
- `instrument()`/`uninstrument()` are both idempotent on a given instrumentor instance — a second
  `instrument()` call is a no-op, and `uninstrument()` removes the wrapper again. Several
  instrumentors (different tracer providers) can be installed at once.
  Not a side effect of importing the module — you activate it explicitly, same as any real
  `opentelemetry-instrumentation-*` package.

Every span carries `db.system.name` (the dialect — `sqlite`, `postgresql`, `clickhouse`; `other_sql` for a
dialect that names none) and, unless disabled,
`db.query.text`; it is a client span (OpenTelemetry's `CLIENT` span type). A failed query's span gets OpenTelemetry's own
default exception recording and `ERROR` status (from `start_as_current_span`'s built-in
`record_exception`/`set_status_on_exception` behavior) — nothing extra to configure.
The exception event's message is `str(exc)`, which names the SQL text but never the bind parameters
(see [The SQL of a database error](../errors/exceptions.md#sqlerrormixin)). The database's own error text is kept as is,
though: PostgreSQL puts the offending key or row into its `DETAIL` line (`Key (email)=(...) already
exists`, `Failing row contains (...)`), so treat exception events as potentially holding row data.

A PostgreSQL bulk `COPY` load (`bulk_create(use_copy=True)`) gets its own `copy` span. It
runs no SQL text, so its `db.query.text` is `COPY <table> (<columns>) FROM STDIN`, never the row
values. A stream (`QuerySet.stream()`) gets a `stream` span covering the whole iteration; it is the
current span only while a row is being fetched, so spans your code opens between rows don't nest
under it.

Deliberately **not** derived from the raw SQL text: `db.operation`/`db.sql.table`. Parsing them out
reliably would need a real SQL parser, and the value is low relative to the effort — a genuine
mismatch with the "don't build fragile heuristics" spirit the rest of hare-orm's instrumentation
follows.

## <a id="combining-with-query-tags"></a>Combining with SQL comment tags

[`QueryTags`](query-tags.md) and this integration are independent — `QueryTags` deliberately has no
dependency on `opentelemetry`. If you want
a tag correlating your SQL comments with the active trace, read it from the current span yourself:

```python
from opentelemetry import trace

from hare.instrumentation import QueryTags

span_context = trace.get_current_span().get_span_context()
with QueryTags.scope(trace_id=format(span_context.trace_id, "032x")):
    ...
```
