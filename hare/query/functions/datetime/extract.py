from __future__ import annotations

from datetime import tzinfo as TzInfo
from typing import Any, ClassVar

from hare.exceptions import FieldError
from hare.fields.data.numeric.int_field import IntField
from hare.query.expressions import Expression, Function
from hare.query.expressions.expression_context import ExpressionContext
from hare.query.expressions.expression_result import ExpressionResult
from hare.query.filters.constants import CALENDAR_DATE_PART_LOOKUPS, DATETIME_DATE_PART_LOOKUPS
from hare.query.functions.datetime.date_function_source import DateFunctionSource
from hare.query.plans.description.declared_plan_parts import DeclaredPlanParts
from hare.query.plans.enums import PlanPartType
from hare.sql.enums import DatePart, DateTruncSource
from hare.sql.functions.datetime.extract import Extract as ExtractTerm
from hare.sql.terms.term import Term
from hare.time import Timezone


class Extract(Function):
    """A date part of a date, time or datetime as an integer - a datetime's in the current zone
    (``Timezone.override()``, else Hare's configured one; the local system zone under
    ``use_timezone=False``) or in ``tzinfo``, like the ``__year``/``__hour`` lookups:
    ``Extract("created_at", "week_day")``. ``week_day`` counts 1 (Sunday) to 7, ``iso_week_day`` 1
    (Monday) to 7, ``week`` and ``iso_year`` follow ISO-8601."""

    #: Its options are part of the key (``get_plan_options()``).
    plan_parts: ClassVar[DeclaredPlanParts] = (
        *Function.plan_parts,
        ("part_name", PlanPartType.NONE),
        ("date_part", PlanPartType.NONE),
        ("zone_name", PlanPartType.NONE),
    )

    #: The part a subclass extracts; ``Extract`` itself takes it as an argument.
    lookup_name: ClassVar[str | None] = None

    populate_field_object = True

    #: Shared, long-lived instance - the statement plans hold an annotation's output
    #: field weakly, so a fresh instance per call would be collected immediately.
    DATE_PART_OUTPUT_FIELD = IntField()

    def __init__(
        self, field: str | Expression | Term, lookup_name: str | None = None, *, tzinfo: str | TzInfo | None = None
    ) -> None:
        """
        Args:
            field: The field name or expression.
            lookup_name: The part - ``year``, ``iso_year``, ``quarter``, ``month``, ``week``,
                ``week_day``, ``iso_week_day``, ``day``, ``hour``, ``minute``, ``second`` or
                ``microsecond``; set by a subclass.
            tzinfo: The zone a datetime's part is taken in - an IANA name or a ``ZoneInfo`` - in
                place of the current zone.

        Raises:
            FieldError: The part is missing or unknown.
            ConfigurationError: ``tzinfo`` isn't a known IANA zone.
        """
        part_name = lookup_name if lookup_name is not None else self.lookup_name
        if part_name not in DATETIME_DATE_PART_LOOKUPS:
            known = ", ".join(DATETIME_DATE_PART_LOOKUPS)
            raise FieldError(f"Extract() part must be one of {known}, got {part_name!r}")
        super().__init__(field)  # type: ignore[arg-type]
        self.date_part: DatePart = DATETIME_DATE_PART_LOOKUPS[part_name]
        self.part_name = part_name
        self.zone_name = None if tzinfo is None else Timezone.get_zone_name(tzinfo)

    def get_plan_options(self) -> tuple[Any, ...]:
        return (self.date_part, self.zone_name)

    def _get_function_field(self, term: Term | str, *default_values: Any) -> ExtractTerm:
        return ExtractTerm(self.date_part, term, as_integer=True, zone_as_literal=True)  # type: ignore[arg-type]

    def get_result(self, expression_context: ExpressionContext) -> ExpressionResult:
        result = super().get_result(expression_context)
        source = DateFunctionSource.get(type(self).__name__, result.output_field)  # type:ignore[call-overload]
        is_calendar_part = self.part_name in CALENDAR_DATE_PART_LOOKUPS
        if (source == DateTruncSource.DATE and not is_calendar_part) or (
            source == DateTruncSource.TIME and is_calendar_part
        ):
            raise FieldError(f"{type(self).__name__}() can't take '{self.part_name}' of a {source.value}")
        extract_term = result.term
        if isinstance(extract_term, ExtractTerm) and source == DateTruncSource.DATETIME:
            use_timezone = Timezone.get_use_timezone()
            extract_term.zone_name = self.zone_name or (Timezone.name() if use_timezone else None)
            extract_term.use_local_zone_when_naive = not use_timezone
        return ExpressionResult(
            term=extract_term,
            joins=result.joins,
            output_field=self.DATE_PART_OUTPUT_FIELD,  # type:ignore[call-overload]
        )

    value_field = DATE_PART_OUTPUT_FIELD
