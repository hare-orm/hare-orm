"""The shared test models whose fields need pydantic - a JSONField whose field_type is a pydantic model, a
model configuring its pydantic schema - registered with the shared models in the "pydantic" environment only."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from hare import fields
from hare.models import Model

REQUIRES = ("pydantic",)


class TestSchemaForJSONField(BaseModel):
    foo: int
    bar: str
    __test__ = False


json_pydantic_default = TestSchemaForJSONField(foo=1, bar="baz")


class PydanticJSONFields(Model):
    """A JSONField whose field_type is a pydantic model - with a default of it."""

    id = fields.IntField(primary_key=True)
    data = fields.JSONField()  # type: ignore
    # Test cases where generics are provided and the type is a pydantic base model
    data_pydantic = fields.JSONField[TestSchemaForJSONField](
        default=json_pydantic_default, field_type=TestSchemaForJSONField
    )


class JSONFieldsDeclaredType(Model):
    """JSONFields whose field_type is a pydantic model and a list of them."""

    id = fields.IntField(primary_key=True)
    item = fields.JSONField[TestSchemaForJSONField](field_type=TestSchemaForJSONField, null=True)
    items = fields.JSONField[list[TestSchemaForJSONField]](field_type=list[TestSchemaForJSONField], null=True)


def camelize_var(var_name: str):
    var_parts: list[str] = var_name.split("_")
    return var_parts[0] + "".join([part.title() for part in var_parts[1:]])


class CamelCaseAliasPerson(Model):
    """CamelCaseAliasPerson model.

    - A model that generates camelized aliases automatically by
        configuring config_class.
    """

    id = fields.IntField(primary_key=True)
    first_name = fields.CharField(max_length=255)
    last_name = fields.CharField(max_length=255)
    full_address = fields.TextField(null=True)

    class PydanticMeta:
        """Defines the default config for pydantic model generator."""

        model_config = ConfigDict(
            title="My custom title",
            extra="ignore",
            alias_generator=camelize_var,
            populate_by_name=True,
        )
