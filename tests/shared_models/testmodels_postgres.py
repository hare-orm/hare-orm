from __future__ import annotations

import datetime
import re
from decimal import Decimal
from enum import StrEnum
from typing import TYPE_CHECKING

from hare import Model, fields
from hare.ddl.raw_sql_term import RawSQLTerm
from hare.fields import ArrayField
from hare.fields.generated_field import GeneratedField
from hare.fields.validators import (
    CommaSeparatedIntegerListValidator,
    MaxLengthValidator,
    MaxValueValidator,
    MinLengthValidator,
    MinValueValidator,
    RegexValidator,
    validate_ipv4_address,
    validate_ipv6_address,
    validate_ipv46_address,
)
from hare.time import UTC

if TYPE_CHECKING:
    from tests.shared_models.testmodels import AggregationShop, AggregationTag, TemporalBatch


class Color(StrEnum):
    RED = "red"
    BLUE = "blue"


class ArrayFields(Model):
    id = fields.IntField(primary_key=True)
    array = ArrayField(base_field=fields.IntField())
    array_null = ArrayField(base_field=fields.IntField(), null=True)
    array_str = ArrayField(base_field=fields.CharField(max_length=1), null=True)
    array_smallint = ArrayField(base_field=fields.SmallIntField(), null=True)
    # A base_field whose own to_db_value/from_db_value do real work (unlike a plain int/str,
    # which round-trip correctly "by accident" even without per-element conversion) - catches
    # ArrayField not applying base_field's conversion to each element.
    array_enum = ArrayField(base_field=fields.CharEnumField(Color, max_length=8), null=True)
    array_max_length_2 = ArrayField(base_field=fields.IntField(), null=True, validators=[MaxLengthValidator(2)])
    array_json = ArrayField(base_field=fields.JSONField(), null=True)
    array_nested = ArrayField(base_field=ArrayField(base_field=fields.IntField()), null=True)
    array_nested_3d = ArrayField(base_field=ArrayField(base_field=ArrayField(base_field=fields.IntField())), null=True)
    array_datetime = ArrayField(base_field=fields.DatetimeField(), null=True)
    array_nested_text = ArrayField(base_field=ArrayField(base_field=fields.TextField()), null=True)


class GeneratedArrayThing(Model):
    id = fields.IntField(primary_key=True)
    a = fields.IntField()
    b = fields.IntField()
    tags = GeneratedField(expression=RawSQLTerm("ARRAY[a, b]"), output_field=ArrayField(base_field=fields.IntField()))


class DecimalFields(Model):
    id = fields.IntField(primary_key=True)
    decimal = fields.DecimalField(max_digits=18, decimal_places=4)
    decimal_nodec = fields.DecimalField(max_digits=18, decimal_places=0)
    decimal_null = fields.DecimalField(max_digits=18, decimal_places=4, null=True)


class HighPrecisionDecimalFields(Model):
    """max_digits above decimal's own default context precision (28) - Postgres NUMERIC itself
    supports far higher precision than that, so DecimalField must not rely on the ambient
    decimal.getcontext() when quantizing a value."""

    id = fields.IntField(primary_key=True)
    big = fields.DecimalField(max_digits=38, decimal_places=18)


class DefaultModel(Model):
    int_default = fields.IntField(db_default=1)
    float_default = fields.FloatField(db_default=1.5)
    decimal_default = fields.DecimalField(max_digits=8, decimal_places=2, db_default=Decimal(1))
    bool_default = fields.BooleanField(db_default=True)
    char_default = fields.CharField(max_length=20, db_default="hare")
    date_default = fields.DateField(db_default=datetime.date(year=2020, month=5, day=21))
    datetime_default = fields.DatetimeField(db_default=datetime.datetime(year=2020, month=5, day=20, tzinfo=UTC))


class ValidatorModel(Model):
    regex = fields.CharField(max_length=100, null=True, validators=[RegexValidator("abc.+", re.I)])
    max_length = fields.CharField(max_length=5, null=True)
    min_length = fields.CharField(max_length=5, null=True, validators=[MinLengthValidator(3)])
    ipv4 = fields.CharField(max_length=100, null=True, validators=[validate_ipv4_address])
    ipv6 = fields.CharField(max_length=100, null=True, validators=[validate_ipv6_address])
    ipv46 = fields.CharField(max_length=100, null=True, validators=[validate_ipv46_address])
    max_value = fields.IntField(null=True, validators=[MaxValueValidator(20.0)])
    min_value = fields.IntField(null=True, validators=[MinValueValidator(10.0)])
    max_value_decimal = fields.DecimalField(
        max_digits=12,
        decimal_places=3,
        null=True,
        validators=[MaxValueValidator(Decimal("2.0"))],
    )
    min_value_decimal = fields.DecimalField(
        max_digits=12,
        decimal_places=3,
        null=True,
        validators=[MinValueValidator(Decimal("1.0"))],
    )
    comma_separated_integer_list = fields.CharField(
        max_length=100, null=True, validators=[CommaSeparatedIntegerListValidator()]
    )


class AggregationProduct(Model):
    shop: fields.ForeignKeyRelation[AggregationShop] = fields.ForeignKeyField(
        "models.AggregationShop", related_name="products"
    )
    name = fields.CharField(max_length=50)
    price = fields.DecimalField(max_digits=10, decimal_places=2)
    qty = fields.IntField()
    weight = fields.FloatField(null=True)
    score = fields.IntField(null=True)
    tags: fields.ManyToManyRelation[AggregationTag] = fields.ManyToManyField(
        "models.AggregationTag", related_name="products"
    )

    reviews: fields.ReverseRelation["AggregationReview"]


class AggregationReview(Model):
    product: fields.ForeignKeyRelation[AggregationProduct] = fields.ForeignKeyField(
        "models.AggregationProduct", related_name="reviews"
    )
    stars = fields.IntField()


class AggregationOrder(Model):
    shop: fields.ForeignKeyRelation[AggregationShop] = fields.ForeignKeyField(
        "models.AggregationShop", related_name="orders"
    )
    total = fields.DecimalField(max_digits=10, decimal_places=2)


class TemporalRecord(Model):
    """One of every date/time field type, for date/time arithmetic tests."""

    id = fields.IntField(primary_key=True)
    started_at = fields.DatetimeField()
    finished_at = fields.DatetimeField(null=True)
    day = fields.DateField()
    other_day = fields.DateField(null=True)
    at_time = fields.TimeField(null=True)
    duration = fields.TimeDeltaField(null=True)
    other_duration = fields.TimeDeltaField(null=True)
    quantity = fields.IntField(null=True)
    amount = fields.DecimalField(max_digits=10, decimal_places=2, null=True)
    updated_at = fields.DatetimeField(auto_now=True)
    batch: fields.ForeignKeyNullableRelation[TemporalBatch] = fields.ForeignKeyField(
        "models.TemporalBatch", related_name="records", null=True
    )


class ExpressionTypeParity(Model):
    """Mixed-type row for checking annotation result types match across backends."""

    num = fields.IntField()
    big = fields.BigIntField()
    dec = fields.DecimalField(max_digits=10, decimal_places=2)
    flag = fields.BooleanField()
    grp = fields.CharField(max_length=20)
    ts = fields.DatetimeField()


class ExpressionTypeRow(Model):
    """Row of every numeric/temporal field type for checking mixed-type expression results."""

    num = fields.IntField()
    fl = fields.FloatField()
    dec = fields.DecimalField(max_digits=10, decimal_places=2)
    dec3 = fields.DecimalField(max_digits=12, decimal_places=3)
    d = fields.DateField()
    dt = fields.DatetimeField()
    td = fields.TimeDeltaField()
    s = fields.CharField(max_length=50)
    flag = fields.BooleanField()
    num_null = fields.IntField(null=True)
    dec_null = fields.DecimalField(max_digits=10, decimal_places=2, null=True)
    children: fields.ReverseRelation["ExpressionTypeChild"]


class ExpressionTypeChild(Model):
    """Child of `ExpressionTypeRow`, for aggregates and subqueries across a relation."""

    row: fields.ForeignKeyRelation[ExpressionTypeRow] = fields.ForeignKeyField(
        "models.ExpressionTypeRow", related_name="children"
    )
    num = fields.IntField()
    dec = fields.DecimalField(max_digits=10, decimal_places=2)
