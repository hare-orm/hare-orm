from __future__ import annotations

from collections.abc import Callable
from copy import copy
from enum import Enum
from typing import TYPE_CHECKING, Any, cast

from pydantic import field_validator

from hare import (
    ForeignKeyFieldInstance,
    OneToOneFieldInstance,
)
from hare.contrib.pydantic.constants import JSON_SCHEMA_ONLY_CONSTRAINTS
from hare.contrib.pydantic.models.pydantic_model import PydanticModel
from hare.exceptions import ValidationError as HareValidationError
from hare.fields import Field
from hare.fields.data.choices.char_enum_field_instance import CharEnumFieldInstance
from hare.fields.data.choices.int_enum_field_instance import IntEnumFieldInstance
from hare.fields.generated_field import GeneratedField

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.contrib.pydantic.creation.pydantic_model_creator import PydanticModelCreator


class DataFieldSchemas:
    """The fields a model's data fields get in the pydantic model: the type a generated field computes,
    a tenant field hidden, a caller-supplied primary key required, and the validators running each
    field's own checks."""

    @staticmethod
    def process_data_field(
        creator: PydanticModelCreator,
        field_name: str,
        field: Field[Any],
        json_schema_extra: dict[str, Any],
        fconfig: dict[str, Any],
    ) -> Any:
        annotation = creator._annotations.get(field_name, None)
        constraints = copy(field.constraints)
        for schema_key in JSON_SCHEMA_ONLY_CONSTRAINTS:
            if schema_key in constraints:
                json_schema_extra[schema_key] = constraints.pop(schema_key)
        python_type = field.get_value_annotation()
        underlying_field = DataFieldSchemas.unwrap_generated_field(field)
        if isinstance(underlying_field, (IntEnumFieldInstance, CharEnumFieldInstance)):
            # The enum already pins the exact allowed values - the column's own numeric range /
            # length constraints next to its $ref are redundant and not valid for an enum schema.
            constraints = {}
        fconfig.update(constraints)
        ptype = python_type
        if field.null:
            json_schema_extra["nullable"] = True
        if field_name in creator._optional or (field.null and not field.pk):
            ptype = ptype | None
        if not (creator._exclude_read_only and json_schema_extra.get("readOnly") is True):
            return annotation or ptype
        return None

    @staticmethod
    def unwrap_generated_field(field: Field[Any]) -> Field[Any]:
        """Unwraps a ``GeneratedField`` to its ``output_field`` - a generated JSON or enum column is
        typed as one.
        """
        return field.output_field if isinstance(field, GeneratedField) else field

    @staticmethod
    def is_tenant_field(creator: PydanticModelCreator, field_name: str, field: Field[Any]) -> bool:
        """Whether ``field`` is ``Meta.tenant_field`` itself or the forward relation owning its
        column.

        Args:
            creator: The creator of the pydantic model.
            field_name: The field's name on the model.
            field: The field.
        """
        tenant_field = creator._model_class._meta.tenant_field
        if tenant_field is None:
            return False
        if field_name == tenant_field:
            return True
        return isinstance(field, (ForeignKeyFieldInstance, OneToOneFieldInstance)) and list(field.source_fields) == [
            tenant_field
        ]

    @staticmethod
    def get_caller_supplied_pk_fields(creator: PydanticModelCreator) -> list[Field[Any]]:
        """The primary key fields a create payload carries. With ``exclude_readonly`` a component the
        database or ORM assigns is left out, and so is one a forward relation covers.

        Args:
            creator: The creator of the pydantic model.

        Returns:
            The primary key fields.
        """
        pk_fields = creator._model_description.pk_fields
        if not creator._exclude_read_only:
            return pk_fields
        relation_source_field_names = {
            source_field_name
            for relation_field in (
                *creator._model_description.foreign_key_fields,
                *creator._model_description.one_to_one_fields,
            )
            for source_field_name in cast("ForeignKeyFieldInstance[Any]", relation_field).source_fields
        }
        return [
            field
            for field in pk_fields
            if not (
                field.generated
                or field.default is not None
                or field.has_db_default()
                or field.model_field_name in relation_source_field_names
            )
        ]

    @staticmethod
    def build_orm_field_validators(creator: PydanticModelCreator) -> dict[str, Any]:
        """A pydantic field validator for every field with ORM ``validators=[...]``, so the schema
        enforces what ``Field.validate()`` does.

        Args:
            creator: The creator of the pydantic model.

        Returns:
            ``create_model``'s ``__validators__``, keyed apart from the caller's own.
        """
        generated_validators: dict[str, Any] = {}
        for field_name, field in creator._field_map.items():
            if isinstance(field, Field) and field.validators and field_name in creator._properties:
                generated_validators[f"_hare_orm_validate_{field_name}"] = field_validator(field_name)(
                    DataFieldSchemas.make_orm_field_validator(field, accepts_none=field_name in creator._optional)
                )
        return generated_validators

    @staticmethod
    def make_orm_field_validator(field: Field[Any], accepts_none: bool) -> Callable[[type[PydanticModel], Any], Any]:
        """Builds the pydantic validator running ``field``'s ORM validators.

        Args:
            field: The ORM field.
            accepts_none: The field is listed in ``optional=`` - its schema allows null, meaning
                "not provided", which the ORM validators must not reject.
        """

        validate_field = field.validate
        validates_enum_text = isinstance(field, CharEnumFieldInstance)

        def validate(cls: type[PydanticModel], value: Any) -> Any:
            if value is None and accepts_none:
                return value
            try:
                if validates_enum_text and isinstance(value, Enum):
                    # Stored (and length-checked) as the text of the member's value.
                    validate_field(str(value.value))
                else:
                    validate_field(value)
            except HareValidationError as error:
                raise ValueError(str(error)) from error
            return value

        return validate
