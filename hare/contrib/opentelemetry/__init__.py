"""OpenTelemetry integration: a span around every query, nested under the active one. Needs the
``opentelemetry`` extra.

Example::

    instrumentor = OpenTelemetryInstrumentor()
    instrumentor.instrument()
"""

from hare.contrib.opentelemetry.instrumentor import OpenTelemetryInstrumentor

__all__ = ("OpenTelemetryInstrumentor",)
