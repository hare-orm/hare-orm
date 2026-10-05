from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any
from urllib.parse import unquote

from pydantic import BeforeValidator, WithJsonSchema

from hare.contrib.request_query.types.constants import GENERIC_TARGET_SEPARATOR, VALUE_SEPARATOR
from hare.contrib.request_query.types.text_parameter import TextParameter
from hare.exceptions import ValidationError as HareValidationError
from hare.fields.constants import GENERIC_FOREIGN_KEY_TYPE_FIELD

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.fields.relations.fields.generic_foreign_key_field_instance import GenericForeignKeyFieldInstance


class GenericTarget:
    """The value of a ``GenericForeignKeyField`` in a request: the branch and the key of its target,
    ``?target=post:1`` - a composite key's values joined with commas (``article_version:7,2``), a
    value holding a comma or a colon written percent-encoded. It reaches the filter as
    ``{"type": "post", "id": 1}``.
    """

    @staticmethod
    def get_annotation(generic_field: GenericForeignKeyFieldInstance[Any]) -> Any:
        """The type of a parameter taking a target of ``generic_field``.

        Args:
            generic_field: The field.

        Returns:
            The annotation.
        """
        examples = ", ".join(
            f"{branch_name}{GENERIC_TARGET_SEPARATOR}<{GenericTarget.get_key_description(generic_field, branch_name)}>"
            for branch_name in generic_field.branch_names
        )
        description = f"The type and the key of the target: {examples}"
        return Annotated[
            dict[str, Any],
            BeforeValidator(lambda value: GenericTarget.read(generic_field, value)),
            WithJsonSchema({"type": "string", "description": description}),
            TextParameter(description),
        ]

    @staticmethod
    def get_key_description(generic_field: GenericForeignKeyFieldInstance[Any], branch_name: str) -> str:
        """How the key of a branch's target is written - its key fields joined with commas."""
        key_fields = generic_field.model._meta.fields_map[branch_name].to_field_instances  # type: ignore[attr-defined]
        return VALUE_SEPARATOR.join(key_field.model_field_name for key_field in key_fields)

    @staticmethod
    def read(generic_field: GenericForeignKeyFieldInstance[Any], value: Any) -> Any:
        """Reads ``<branch>:<key>`` text as ``{"type": "<branch>", <key field>: <value>, ...}``.

        Args:
            generic_field: The field.
            value: The parameter's value - anything but text is left to validation.

        Returns:
            The branch and the key values.

        Raises:
            ValueError: The text names no branch, or its key doesn't fit the branch's target.
        """
        if not isinstance(value, str):
            return value
        branch_name, separator, key_text = value.partition(GENERIC_TARGET_SEPARATOR)
        if not separator or branch_name not in generic_field.branch_names:
            raise ValueError(
                f"expected <type>{GENERIC_TARGET_SEPARATOR}<key>, the type one of "
                f"{', '.join(generic_field.branch_names)} - got {value!r}"
            )
        key_fields = generic_field.model._meta.fields_map[branch_name].to_field_instances  # type: ignore[attr-defined]
        key_parts = [unquote(part) for part in key_text.split(VALUE_SEPARATOR)]
        if len(key_parts) != len(key_fields):
            raise ValueError(
                f"the key of {branch_name!r} is {GenericTarget.get_key_description(generic_field, branch_name)} - "
                f"got {key_text!r}"
            )
        key_values: dict[str, Any] = {GENERIC_FOREIGN_KEY_TYPE_FIELD: branch_name}
        for key_field, key_part in zip(key_fields, key_parts, strict=True):
            try:
                key_values[key_field.model_field_name] = key_field.to_python(key_part)
            except HareValidationError as error:
                raise ValueError(f"the key {key_part!r} of {branch_name!r}: {error}") from None
        return key_values
