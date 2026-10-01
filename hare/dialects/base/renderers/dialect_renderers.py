from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.sql.context import SqlContext

    #: ``(term, ctx) -> sql``.
    TermRenderer = Callable[[Any, SqlContext], str]
    #: ``(function, ctx) -> name`` - an empty name keeps the function's own.
    FunctionNameRenderer = Callable[[Any, SqlContext], str]
from hare.dialects.base.renderers.term_renderers import TermRenderers


class DialectRenderers:
    """How one of hare's own dialects renders the terms whose SQL differs between dialects - a class
    method per term. ``build()`` registers the shared renderers, then the dialect's own. A
    third-party dialect starts from an empty ``TermRenderers``.
    """

    @classmethod
    def build(cls) -> TermRenderers:
        """The dialect's term renderers.

        Returns:
            The renderers.
        """
        # Local import: hare.sql imports the dialects package.
        from hare.sql import functions
        from hare.sql.terms import criteria, functions as term_functions

        renderers = TermRenderers()
        for term_class, method_name in (
            (functions.BooleanAsText, "render_boolean_as_text"),
            (functions.FloatAsText, "render_float_as_text"),
            (functions.DecimalAsText, "render_decimal_as_text"),
            (functions.DatetimeAsText, "render_datetime_as_text"),
            (functions.Extract, "render_extract"),
            (functions.MathFunction, "render_math_function"),
            (functions.CastTo, "render_cast_to"),
            (functions.TextFunction, "render_text_function"),
            (functions.DateTrunc, "render_date_trunc"),
            (functions.DatetimeCast, "render_datetime_cast"),
            (functions.TemporalShift, "render_temporal_shift"),
            (functions.TemporalDifference, "render_temporal_difference"),
            (functions.JsonObject, "render_json_object"),
            (functions.JsonValue, "render_json_value"),
            (functions.JsonComparand, "render_json_comparand"),
            (functions.DateAsTimestamp, "render_date_as_timestamp"),
            (functions.JsonSortKey, "render_json_sort_key"),
            (term_functions.Interval, "render_interval"),
            (term_functions.FloatMod, "render_float_mod"),
            (criteria.JSONAttributeCriterion, "render_json_attribute"),
            (criteria.JSONTypeCriterion, "render_json_type"),
        ):
            renderers.register(term_class, getattr(cls, method_name))
        cls.add_own_renderers(renderers)
        return renderers

    @classmethod
    def add_own_renderers(cls, renderers: TermRenderers) -> None:
        """Adds the renderers only this dialect has. Adds none by default.

        Args:
            renderers: The renderers being built.
        """
