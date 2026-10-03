from typing import TYPE_CHECKING, Any

from hare.fields.base.field import Field
from hare.fields.composite_primary_key import CompositePrimaryKey
from hare.fields.constants import CASCADE, NO_ACTION, PROTECT, RESTRICT, SET_DEFAULT, SET_NULL
from hare.fields.data.binary import BinaryField
from hare.fields.data.boolean import BooleanField
from hare.fields.data.choices.char_enum_field import CharEnumField
from hare.fields.data.choices.int_enum_field import IntEnumField
from hare.fields.data.json.json_field import JSONField
from hare.fields.data.numeric.big_int_field import BigIntField
from hare.fields.data.numeric.decimal_field import DecimalField
from hare.fields.data.numeric.float_field import FloatField
from hare.fields.data.numeric.int_field import IntField
from hare.fields.data.numeric.positive_big_int_field import PositiveBigIntField
from hare.fields.data.numeric.positive_int_field import PositiveIntField
from hare.fields.data.numeric.positive_small_int_field import PositiveSmallIntField
from hare.fields.data.numeric.small_int_field import SmallIntField
from hare.fields.data.temporal.date_field import DateField
from hare.fields.data.temporal.datetime_field import DatetimeField
from hare.fields.data.temporal.time_delta_field import TimeDeltaField
from hare.fields.data.temporal.time_field import TimeField
from hare.fields.data.text.char_field import CharField
from hare.fields.data.text.text_field import TextField
from hare.fields.data.uuids import UUIDField
from hare.fields.db_defaults.now import Now
from hare.fields.db_defaults.random_hex import RandomHex
from hare.fields.db_defaults.sql_default import SqlDefault
from hare.fields.encrypted.encrypted_json_field import EncryptedJSONField
from hare.fields.encrypted.encrypted_text_field import EncryptedTextField
from hare.fields.enums import OnDelete, RelationLoadStrategy, RelationType
from hare.fields.generated import GeneratedField
from hare.fields.relations.fields.backward_fk_relation import BackwardFKRelation
from hare.fields.relations.fields.backward_one_to_one_relation import BackwardOneToOneRelation
from hare.fields.relations.fields.foreign_key_field import ForeignKeyField
from hare.fields.relations.fields.foreign_key_field_instance import ForeignKeyNullableRelation, ForeignKeyRelation
from hare.fields.relations.fields.many_to_many_field import ManyToManyField
from hare.fields.relations.fields.one_to_one_field import OneToOneField
from hare.fields.relations.fields.one_to_one_field_instance import OneToOneNullableRelation, OneToOneRelation

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.query.queryset.relations.many_to_many_relation import ManyToManyRelation
    from hare.query.queryset.relations.reverse_relation import ReverseRelation


def __getattr__(name: str) -> Any:
    # The relation querysets are built on the queryset package, which imports the field modules -
    # they are read from it on first use rather than while this package loads.
    if name == "ManyToManyRelation":
        from hare.query.queryset.relations.many_to_many_relation import ManyToManyRelation

        return ManyToManyRelation
    if name == "ReverseRelation":
        from hare.query.queryset.relations.reverse_relation import ReverseRelation

        return ReverseRelation
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "CASCADE",
    "RESTRICT",
    "SET_DEFAULT",
    "SET_NULL",
    "NO_ACTION",
    "PROTECT",
    "OnDelete",
    "RelationType",
    "CompositePrimaryKey",
    "Field",
    "GeneratedField",
    "Now",
    "RandomHex",
    "SqlDefault",
    "BigIntField",
    "BinaryField",
    "BooleanField",
    "CharEnumField",
    "CharField",
    "DateField",
    "DatetimeField",
    "TimeField",
    "DecimalField",
    "EncryptedJSONField",
    "EncryptedTextField",
    "FloatField",
    "IntEnumField",
    "IntField",
    "JSONField",
    "PositiveBigIntField",
    "PositiveIntField",
    "PositiveSmallIntField",
    "SmallIntField",
    "TextField",
    "TimeDeltaField",
    "UUIDField",
    "BackwardFKRelation",
    "BackwardOneToOneRelation",
    "ForeignKeyField",
    "ForeignKeyNullableRelation",
    "ForeignKeyRelation",
    "ManyToManyField",
    "ManyToManyRelation",
    "OneToOneField",
    "OneToOneNullableRelation",
    "OneToOneRelation",
    "RelationLoadStrategy",
    "ReverseRelation",
]
