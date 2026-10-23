"""OpenTelemetry integration: a span around every query, nested under the active one, and the metrics
of the pools of connections. Needs the ``opentelemetry`` extra.

Example::

    instrumentor = OpenTelemetryInstrumentor()
    instrumentor.instrument()
"""

from __future__ import annotations

from hare.contrib.opentelemetry.open_telemetry_instrumentor import OpenTelemetryInstrumentor
from hare.contrib.opentelemetry.pool_metric_instruments import PoolMetricInstruments
from hare.contrib.opentelemetry.query_spans import QuerySpans

__all__ = ("OpenTelemetryInstrumentor", "PoolMetricInstruments", "QuerySpans")
