"""The type an average, a standard deviation or a variance of integers comes back as on the database
under test."""

from decimal import Decimal

from hare.models import Model


class IntegerStatistic:
    """What the database under test computes a statistic of integers as."""

    @staticmethod
    def get_type(model: type[Model]) -> type:
        """``Decimal`` where the database computes it exactly (PostgreSQL's ``numeric``), else ``float``."""
        return Decimal if model._meta.connection.dialect.features.computes_integer_statistics_exactly else float
