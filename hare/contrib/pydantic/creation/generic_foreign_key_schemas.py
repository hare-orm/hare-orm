from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any, Literal, Union, cast

from pydantic import Discriminator, Field as PydanticField, Tag, create_model

from hare.contrib.pydantic.models.pydantic_model import PydanticModel
from hare.exceptions import NoValuesFetched
from hare.fields.constants import GENERIC_FOREIGN_KEY_TYPE_FIELD
from hare.query.queryset import QuerySet
from hare.query.queryset.single_rows.none_awaitable_type import NoneAwaitable

if TYPE_CHECKING:  # pragma: nocoverage
    from collections.abc import Callable

    from hare.fields.relations.fields.generic_foreign_key_field_instance import GenericForeignKeyFieldInstance


class GenericForeignKeySchemas:
    """The schema of a ``GenericForeignKeyField``: on output a union of its targets' schemas told
    apart by a ``type`` field holding the branch name; on input - and with ``relations_as_ids`` -
    ``{"type": "<branch>", <the target's key fields>}``."""

    @staticmethod
    def get_object_union(
        generic_field: GenericForeignKeyFieldInstance[Any], submodels: dict[str, type[PydanticModel]], schema_name: str
    ) -> Any:
        """The output type: each target's schema with a ``type`` field naming its branch.

        Args:
            generic_field: The field.
            submodels: The schema of each branch's target, by branch name.
            schema_name: The name of the schema the field is in, prefixing the new schemas.

        Returns:
            The annotation.
        """
        branch_models = {
            branch_name: create_model(
                f"{schema_name}.{generic_field.model_field_name}.{branch_name}",
                __base__=submodel,
                **{GENERIC_FOREIGN_KEY_TYPE_FIELD: (Literal[branch_name], branch_name)},  # type: ignore[call-overload]
            )
            for branch_name, submodel in submodels.items()
        }
        return GenericForeignKeySchemas.get_branches_annotation(generic_field, branch_models)

    @staticmethod
    def get_key_union(generic_field: GenericForeignKeyFieldInstance[Any], schema_name: str) -> Any:
        """The input type: ``{"type": "<branch>", <the target's key fields>}`` per branch.

        Args:
            generic_field: The field.
            schema_name: The name of the schema the field is in, prefixing the new schemas.

        Returns:
            The annotation.
        """
        fields_map = generic_field.model._meta.fields_map
        branch_models = {}
        for branch_name in generic_field.branch_names:
            key_fields = fields_map[branch_name].to_field_instances  # type: ignore[attr-defined]
            branch_models[branch_name] = create_model(
                f"{schema_name}.{generic_field.model_field_name}.{branch_name}.key",
                __config__={"extra": "forbid"},
                **{GENERIC_FOREIGN_KEY_TYPE_FIELD: (Literal[branch_name], ...)},  # type: ignore[call-overload]
                **{key_field.model_field_name: (key_field.field_type, ...) for key_field in key_fields},
            )
        return GenericForeignKeySchemas.get_branches_annotation(generic_field, branch_models)

    @staticmethod
    def get_branches_annotation(
        generic_field: GenericForeignKeyFieldInstance[Any], branch_models: dict[str, type[Any]]
    ) -> Any:
        """The schemas of the branches as one type: a union told apart by the branch, or the one
        branch's schema alone - a discriminator takes a union of two or more.

        Args:
            generic_field: The field.
            branch_models: The schema of each branch, by branch name.

        Returns:
            The annotation.
        """
        if len(branch_models) == 1:
            return next(iter(branch_models.values()))
        branch_types = tuple(
            Annotated[branch_model, Tag(branch_name)] for branch_name, branch_model in branch_models.items()
        )
        return Annotated[Union[branch_types], Discriminator(GenericForeignKeySchemas.get_tag_reader(generic_field))]

    @staticmethod
    def get_tag_reader(generic_field: GenericForeignKeyFieldInstance[Any]) -> Callable[[Any], str | None]:
        """Reads the branch of a value: a dict's or a schema's ``type``, a model instance's model.

        Args:
            generic_field: The field.

        Returns:
            The reader.
        """

        def read_tag(value: Any) -> str | None:
            if isinstance(value, dict):
                return cast("str | None", value.get(GENERIC_FOREIGN_KEY_TYPE_FIELD))
            if hasattr(value, "_meta"):
                return generic_field.branch_by_model.get(type(value))
            return getattr(value, GENERIC_FOREIGN_KEY_TYPE_FIELD, None)

        return read_tag

    @staticmethod
    def get_validator(generic_field: GenericForeignKeyFieldInstance[Any], as_keys: bool) -> Callable[[Any, Any], Any]:
        """The ``before`` validator of the field: reads the related object of a model instance as
        the schema takes it.

        Args:
            generic_field: The field.
            as_keys: Whether the schema takes the key fields rather than the object.

        Returns:
            The validator.
        """

        def validate(cls: type[PydanticModel], value: Any) -> Any:
            if value is NoneAwaitable:
                return None
            if isinstance(value, QuerySet) or (hasattr(value, "__await__") and not hasattr(value, "_meta")):
                raise NoValuesFetched(
                    f"Field '{generic_field.model_field_name}' has not been fetched - prefetch its branches "
                    f"({', '.join(generic_field.branch_names)}) or use "
                    f"select_related('{generic_field.model_field_name}') "
                    "before serializing."
                )
            if as_keys and hasattr(value, "_meta"):
                return generic_field.get_key_values(value)
            return value

        return validate

    @staticmethod
    def get_field_info(generic_field: GenericForeignKeyFieldInstance[Any], optional: bool) -> Any:
        """The pydantic field of the generic foreign key.

        Args:
            generic_field: The field.
            optional: Whether the value may be left out.

        Returns:
            The field info.
        """
        title = generic_field.model_field_name.replace("_", " ").title()
        if generic_field.null or optional:
            return PydanticField(default=None, title=title, json_schema_extra={"nullable": True})
        return PydanticField(title=title)
