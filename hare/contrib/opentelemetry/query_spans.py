from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from typing import TYPE_CHECKING, Any

from opentelemetry import trace
from opentelemetry.trace import SpanKind as SpanType

from hare.contrib.opentelemetry.constants import DB_QUERY_TEXT_ATTRIBUTE, DB_SYSTEM_NAME_ATTRIBUTE
from hare.instrumentation.queries.query_wrapper import QueryWrapper

if TYPE_CHECKING:
    from opentelemetry.trace import Span, TracerProvider

    from hare.instrumentation.declarations import QueryCall


class QuerySpans(QueryWrapper):
    """Opens an OpenTelemetry client span around every query-executing call - a query wrapper, so
    the span is current for the whole driver call and nests correctly; a query observer can read it
    with ``opentelemetry.trace.get_current_span()``.

    Args:
        tracer_provider: The TracerProvider to get a tracer from - the global one by default.
        record_sql_statement: Whether the ``db.query.text`` span attribute holds the (parameter-bound,
            never literal-holding) SQL text.
    """

    def __init__(self, tracer_provider: TracerProvider | None = None, *, record_sql_statement: bool = True) -> None:
        self.tracer = trace.get_tracer(__name__, tracer_provider=tracer_provider)
        self.record_sql_statement = record_sql_statement

    def set_span_attributes(self, span: Span, call: QueryCall) -> None:
        """Names the database system and, unless switched off, the SQL on the span.

        Args:
            span: The span.
            call: The call.
        """
        span.set_attribute(DB_SYSTEM_NAME_ATTRIBUTE, call.dialect.otel_system_name)
        if self.record_sql_statement:
            span.set_attribute(DB_QUERY_TEXT_ATTRIBUTE, call.sql)

    async def around(self, call: QueryCall, proceed: Callable[[], Awaitable[Any]]) -> Any:
        # start_as_current_span() records the exception and an ERROR status by itself when the
        # call raises.
        with self.tracer.start_as_current_span(call.method_name, None, SpanType.CLIENT) as span:
            self.set_span_attributes(span, call)
            return await proceed()

    async def around_stream(self, call: QueryCall, proceed: Callable[[], AsyncIterator[Any]]) -> AsyncIterator[Any]:
        # The span covers the whole iteration but is current only while a batch of rows is fetched -
        # never while the generator waits at a yield for the caller, or an unrelated span started
        # then would nest under it (and a caller that stops reading early would leave it current).
        span = self.tracer.start_span(call.method_name, None, SpanType.CLIENT)
        self.set_span_attributes(span, call)
        batches = proceed()
        try:
            while True:
                with trace.use_span(span, end_on_exit=False):
                    try:
                        batch = await anext(batches)
                    except StopAsyncIteration:
                        break
                yield batch
        finally:
            span.end()
            if (close_batches := getattr(batches, "aclose", None)) is not None:
                await close_batches()
