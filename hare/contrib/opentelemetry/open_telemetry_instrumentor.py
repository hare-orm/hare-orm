"""OpenTelemetry for hare-orm: spans around its queries and the metrics of its pools of connections."""

from __future__ import annotations

from typing import TYPE_CHECKING

from hare.contrib.opentelemetry.pool_metric_instruments import PoolMetricInstruments
from hare.contrib.opentelemetry.query_spans import QuerySpans
from hare.instrumentation.observers.observers import Observers

if TYPE_CHECKING:
    from opentelemetry.metrics import MeterProvider
    from opentelemetry.trace import TracerProvider


class OpenTelemetryInstrumentor:
    """Opens an OpenTelemetry client span around every query (``QuerySpans``) and reports the metrics
    of every pool of connections (``PoolMetricInstruments``). Several instrumentors may be installed
    at once.

    Args:
        tracer_provider: The TracerProvider to get a tracer from - defaults to the global one
            (``opentelemetry.trace.get_tracer_provider()``).
        meter_provider: The MeterProvider to get a meter from - defaults to the global one
            (``opentelemetry.metrics.get_meter_provider()``).
        record_sql_statement: When ``True`` (the default), sets the ``db.query.text`` span
            attribute to the (already parameter-bound, never containing literal values) SQL text.
            Set ``False`` to omit it if even the query structure is sensitive.
        query_spans: Whether the queries get spans.
        pool_metrics: Whether the pools are reported - it measures the waits for a connection too
            (``PoolMetrics``).
    """

    def __init__(
        self,
        tracer_provider: TracerProvider | None = None,
        meter_provider: MeterProvider | None = None,
        *,
        record_sql_statement: bool = True,
        query_spans: bool = True,
        pool_metrics: bool = True,
    ) -> None:
        self.query_spans = (
            QuerySpans(tracer_provider, record_sql_statement=record_sql_statement) if query_spans else None
        )
        self.pool_metric_instruments = PoolMetricInstruments(meter_provider) if pool_metrics else None

    def instrument(self) -> None:
        """Starts the spans and the metrics - nothing when already started."""
        if self.query_spans is not None:
            Observers.wrap_queries(self.query_spans)
        if self.pool_metric_instruments is not None:
            self.pool_metric_instruments.start()

    def uninstrument(self) -> None:
        """Stops the spans and the metrics - nothing when not started."""
        if self.query_spans is not None:
            Observers.unwrap_queries(self.query_spans)
        if self.pool_metric_instruments is not None:
            self.pool_metric_instruments.stop()
