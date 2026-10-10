from __future__ import annotations

from datetime import tzinfo as TzInfo
from typing import Any, ClassVar

from hare.exceptions import FieldError
from hare.fields.data.temporal.date_field import DateField
from hare.fields.data.temporal.time_field import TimeField
from hare.fields.field import Field
from hare.query.expressions import Expression, Function
from hare.query.expressions.expression_context import ExpressionContext
from hare.query.expressions.expression_result import ExpressionResult
from hare.query.functions.datetime.date_function_source import DateFunctionSource
from hare.query.plans.description.declared_plan_parts import DeclaredPlanParts
from hare.query.plans.enums import PlanPartType
from hare.sql.enums import DateTruncSource, TruncType
from hare.sql.functions.datetime.date_trunc import DateTrunc
from hare.sql.terms.term import Term
from hare.time import Timezone


class Trunc(Function):
    """A date, time or datetime truncated to ``type`` - a datetime in the current zone
    (``Timezone.override()``, else Hare's configured one) or in ``tzinfo``, coming back as the
    moment it starts: ``Trunc("created_at", "month", tzinfo="Europe/Moscow")``. ``week`` starts on
    Monday; ``date``/``time`` take a datetime's date or time of day."""

    #: Its options are part of the key (``get_plan_options()``).
    plan_parts: ClassVar[DeclaredPlanParts] = (
        *Function.plan_parts,
        ("truncation", PlanPartType.NONE),
        ("zone_name", PlanPartType.NONE),
    )

    #: The type a subclass truncates to; ``Trunc`` itself takes it as an argument.
    trunc_type: ClassVar[str | None] = None

    #: Calendar types, which a time of day has none of.
    CALENDAR_TYPES: ClassVar[frozenset[TruncType]] = frozenset(
        {TruncType.YEAR, TruncType.QUARTER, TruncType.MONTH, TruncType.WEEK, TruncType.DAY, TruncType.DATE}
    )

    #: Shared, long-lived instances - see ``Extract.DATE_PART_OUTPUT_FIELD``.
    DATE_OUTPUT_FIELD = DateField()
    TIME_OUTPUT_FIELD = TimeField()

    populate_field_object = True

    def __init__(
        self, field: str | Expression | Term, trunc_type: str | None = None, *, tzinfo: str | TzInfo | None = None
    ) -> None:
        """
        Args:
            field: The field name or expression.
            trunc_type: ``year``, ``quarter``, ``month``, ``week``, ``day``, ``hour``,
                ``minute``, ``second``, ``date`` or ``time``; a subclass sets its own.
            tzinfo: The zone a datetime is truncated in - an IANA name or a ``ZoneInfo`` - in
                place of the current zone.

        Raises:
            FieldError: The type is missing or unknown.
            ConfigurationError: ``tzinfo`` isn't a known IANA zone.
        """
        trunc_type_name = trunc_type if trunc_type is not None else self.trunc_type
        known_types = [str(member) for member in TruncType]
        if trunc_type_name not in known_types:
            raise FieldError(f"Trunc() type must be one of {', '.join(known_types)}, got {trunc_type_name!r}")
        super().__init__(field)  # type: ignore[arg-type]
        self.truncation = TruncType(str(trunc_type_name))
        self.zone_name = None if tzinfo is None else Timezone.get_zone_name(tzinfo)

    def get_plan_options(self) -> tuple[Any, ...]:
        return (self.truncation, self.zone_name)

    def _get_function_field(self, term: Term | str, *default_values: Any) -> DateTrunc:
        return DateTrunc(self.truncation, term, DateTruncSource.DATETIME)  # type: ignore[arg-type]

    def _raise_if_type_unsupported(self, source: DateTruncSource) -> None:
        """Rejects a type the source has no such part of.

        Raises:
            FieldError: A time part of a date, or a calendar part of a time of day.
        """
        is_calendar_type = self.truncation in self.CALENDAR_TYPES
        if (source == DateTruncSource.DATE and not is_calendar_type) or (
            source == DateTruncSource.TIME and is_calendar_type and self.truncation != TruncType.TIME
        ):
            raise FieldError(f"{type(self).__name__}() can't truncate a {source.value} to '{self.truncation.value}'")
        if source == DateTruncSource.TIME and self.truncation == TruncType.DATE:
            raise FieldError(f"{type(self).__name__}() can't take the date of a time")

    def get_result(self, expression_context: ExpressionContext) -> ExpressionResult:
        result = super().get_result(expression_context)
        source_field: Field[Any] = result.output_field  # type:ignore[call-overload,assignment]
        source = DateFunctionSource.get(type(self).__name__, source_field)
        self._raise_if_type_unsupported(source)
        trunc_term = result.term
        if isinstance(trunc_term, DateTrunc):
            trunc_term.source = source
            if source == DateTruncSource.DATETIME:
                trunc_term.zone_name = self.zone_name or (
                    Timezone.name() if Timezone.get_use_timezone() else Timezone.get_local_zone_name()
                )
        output_field = self._get_truncated_output_field(source_field, source)
        self.field_object = output_field
        return ExpressionResult(term=trunc_term, joins=result.joins, output_field=output_field)

    def _get_truncated_output_field(self, source_field: Field[Any], source: DateTruncSource) -> Field[Any]:
        """The field the truncated value is decoded through."""
        if source == DateTruncSource.DATETIME and self.truncation == TruncType.DATE:
            return self.DATE_OUTPUT_FIELD  # type:ignore[call-overload]
        if source == DateTruncSource.DATETIME and self.truncation == TruncType.TIME:
            return self.TIME_OUTPUT_FIELD  # type:ignore[call-overload]
        return source_field

    def get_value_field(self, result: ExpressionResult) -> Field[Any] | None:
        return result.output_field  # type:ignore[call-overload]
