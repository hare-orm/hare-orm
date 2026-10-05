"""Lookups and a transform registered outside the models module - the mypy plugin imports this module
through ``[tool.hare] mypy_imports`` before it binds the models."""

from functools import partial

from hare import fields
from hare.query.enums import LookupValueShape
from hare.query.expressions import Function
from hare.query.queryset.extensions import QuerySetExtensions
from tests.contrib.mypy.models import RatingField

RatingField.register_lookup("rated_above", RatingField.build_between, value_type=int)
RatingField.register_lookup(
    "rated_on_postgresql",
    RatingField.build_between,
    value_shape=LookupValueShape.RANGE,
    value_type=int,
    dialects=("postgresql",),
)
#: A path segment reading the rating as text - its lookups take strings.
RatingField.register_transform("as_text", lambda field: (partial(Function, "PRINTF"), fields.CharField(max_length=20)))


def first_rows(builder, name_below, *, inclusive=False):
    """A QuerySet method of the typing models' dialect - the plugin declares it."""
    return builder


QuerySetExtensions.register("first_rows", "sqlite", first_rows)
QuerySetExtensions.register("only_on_postgresql", "postgresql", first_rows)
