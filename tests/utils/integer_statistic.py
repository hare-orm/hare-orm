"""The type an average, a standard deviation or a variance of integers comes back as on the database
under test."""

from decimal import Decimal

from hare.core.hare_context import HareContext
from hare.models import Model


class IntegerStatistic:
    """What the database under test computes a statistic of integers as."""

    @staticmethod
    def get_type(model: type[Model]) -> type:
        """``Decimal`` where the database computes it exactly (PostgreSQL's ``numeric``), else ``float``."""
        return (
            Decimal
            if HareContext.require_current()
            .apps.get_queryset(model)
            .get_connection()
            .dialect.features.computes_integer_statistics_exactly
            else float
        )
