from __future__ import annotations

from typing import TYPE_CHECKING

from hare.dialects.base.renderers.term_renderers import TermRenderers

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.dialect import Dialect


class CheckedTermRenderers(TermRenderers):
    """Term renderers checked to be complete: the dialect registers its renderers when they are
    built (``add_own_renderers()``), and each term whose SQL differs between dialects is checked to
    have one - a dialect that forgot a term fails as it is built, not at the first query using the
    term. hare's own dialects build theirs on it; a dialect rendering only some of the terms starts
    from a plain ``TermRenderers``.
    """

    def __init__(self, dialect: Dialect) -> None:
        """
        Args:
            dialect: The dialect whose terms are rendered.

        Raises:
            TypeError: The dialect renders none of a term every hare dialect renders.
        """
        super().__init__(dialect)
        self.add_own_renderers()
        # Local import: hare.sql imports the dialects package.
        from hare.sql import functions
        from hare.sql.terms import criteria, functions as term_functions

        missing_term_classes = [
            term_class.__name__
            for term_class in (
                functions.BooleanAsText,
                functions.FloatAsText,
                functions.DecimalAsText,
                functions.DatetimeAsText,
                functions.Extract,
                functions.MathFunction,
                functions.CastTo,
                functions.TextFunction,
                functions.DateTrunc,
                functions.DatetimeCast,
                functions.TemporalShift,
                functions.TemporalDifference,
                functions.JsonObject,
                functions.JsonArray,
                functions.JsonValue,
                functions.JsonComparand,
                functions.DateAsTimestamp,
                functions.JsonSortKey,
                term_functions.Interval,
                term_functions.FloatMod,
                criteria.JSONAttributeCriterion,
                criteria.JSONTypeCriterion,
            )
            if term_class not in self.renderers
        ]
        if missing_term_classes:
            raise TypeError(f"{type(self).__name__} renders none of {', '.join(missing_term_classes)}")

    def add_own_renderers(self) -> None:
        """Adds the renderers of this dialect. Adds none by default."""
