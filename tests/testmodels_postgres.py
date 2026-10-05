from enum import StrEnum

from hare import Model, fields
from hare.ddl.raw_sql_term import RawSQLTerm
from hare.fields import ArrayField
from hare.fields.generated_field import GeneratedField
from hare.fields.validators import MaxLengthValidator


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
