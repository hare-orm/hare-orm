"""Lookups and a transform registered outside the models module - the mypy plugin imports this module
through ``[tool.hare] mypy_imports`` before it binds the models."""

from functools import partial

from hare import fields
from hare.dialects.dialect_registry import DialectRegistry
from hare.dialects.postgresql.postgresql_dialect import PostgresqlDialect
from hare.dialects.sqlite.sqlite_dialect import SqliteDialect
from hare.query.enums import LookupValueShape
from hare.query.expressions import Function
from hare.query.filters import FieldLookup
from hare.query.filters.lookups.dialect_implemented_operators import DialectImplementedOperators
from hare.query.filters.lookups.lookups import Lookups
from hare.query.queryset.extensions import QuerySetExtensions
from tests.contrib.mypy.models import RatingField

RatingField.register_lookup("rated_above", RatingField.build_between, value_type=int)


@DialectImplementedOperators.mark
def between_on_postgresql(term, value):
    return Lookups.between(term, value)


RatingField.register_lookup(
    "rated_on_postgresql",
    lambda field: FieldLookup(between_on_postgresql),
    value_shape=LookupValueShape.RANGE,
    value_type=int,
)
DialectRegistry.get_dialect("postgresql").filter_operators.register_operator(
    between_on_postgresql, between_on_postgresql
)
#: A path segment reading the rating as text - its lookups take strings.
RatingField.register_transform("as_text", lambda field: (partial(Function, "PRINTF"), fields.CharField(max_length=20)))


def first_rows(builder, name_below, *, inclusive=False):
    """A QuerySet method of the typing models' dialect - the plugin declares it."""
    return builder


QuerySetExtensions.register("first_rows", SqliteDialect, first_rows)
QuerySetExtensions.register("only_on_postgresql", PostgresqlDialect, first_rows)
