"""The public functions building a Pydantic model off a Hare model, and a list model of its rows."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from pydantic import ConfigDict, Field as PydanticField, create_model

from hare.contrib.pydantic.constants import PYDANTIC_MODELS_MODULE
from hare.contrib.pydantic.creation.field_descriptions import FieldDescriptions
from hare.contrib.pydantic.creation.pydantic_model_creator import PydanticModelCreator
from hare.contrib.pydantic.models.pydantic_list_model import PydanticListModel
from hare.contrib.pydantic.models.pydantic_model import PydanticModel

if TYPE_CHECKING:
    from hare.models import Model


def pydantic_model_creator(
    model: type[Model],
    *,
    name: str | None = None,
    exclude: tuple[str, ...] | None = None,
    include: tuple[str, ...] | None = None,
    computed: tuple[str, ...] | None = None,
    extra_fields: dict[str, Any] | None = None,
    related_values: dict[str, str] | None = None,
    optional: tuple[str, ...] | None = None,
    allow_cycles: bool | None = None,
    sort_alphabetically: bool | None = None,
    exclude_readonly: bool = False,
    meta_override: type | None = None,
    model_config: ConfigDict | None = None,
    validators: dict[str, Any] | None = None,
    module: str = PYDANTIC_MODELS_MODULE,
    exclude_sensitive: bool = False,
    relations_as_ids: bool = False,
) -> type[PydanticModel]:
    """Builds a Pydantic model (https://docs.pydantic.dev/latest/concepts/models/) off a Hare
    model.

    Args:
        model: The Hare Model.
        name: Specify a custom name explicitly, instead of a generated name.
        exclude: Extra fields to exclude from the provided model.
        include: Extra fields to include from the provided model.
        computed: Extra computed fields to include from the provided model.
        extra_fields: Values read from a row by name - a query's annotation, an attribute set on the row
            after it was read - with their types; None where a row has none. A path through the
            relations (``"author.books_count"``) adds one to the related schema.
        related_values: Values of related rows, flattened - a name to the path of the related field
            through relations to one row (``{"author_name": "author.name"}``); None where a relation
            wasn't loaded.
        optional: Extra optional fields for the provided model.
        allow_cycles: Whether to allow cycles in the generated model - only useful for
            recursive/self-referential models. ``False`` (the default) prevents any backtracking.
        sort_alphabetically: Sort the parameters alphabetically instead of field-definition
            order. The default order is: field definition order, then order of reverse relations
            (as discovered), then order of computed functions (as provided).
        exclude_readonly: Build a subset model that excludes any readonly fields.
        meta_override: A PydanticMeta class to override the model's values.
        model_config: A custom config to use as pydantic config.
        validators: A dictionary of methods that validate fields.
        module: The name of the module that the model belongs to. Note: the generated pydantic
            model uses this parameter's config_class and PydanticMeta's own config_class as its
            Config class's bases (only if provided), but ignores a ``fields`` config -
            pydantic_model_creator generates fields from include/exclude/computed automatically.
        exclude_sensitive: Drop every ``sensitive=True`` field (``Model._meta.sensitive_fields``),
            nested models' included - e.g. for a public/export schema.
        relations_as_ids: Render every forward FK/O2O as its flat ``<name>_id`` field (the
            target's pk type, the relation's own nullability) instead of a nested submodel, and
            leave reverse relations out. M2M relations stay nested; nested models inherit the flag.
    """
    creator = PydanticModelCreator(
        model=model,
        name=name,
        exclude=exclude,
        include=include,
        computed=computed,
        extra_fields=extra_fields,
        related_values=related_values,
        optional=optional,
        allow_cycles=allow_cycles,
        sort_alphabetically=sort_alphabetically,
        exclude_readonly=exclude_readonly,
        meta_override=meta_override,
        model_config=model_config,
        validators=validators,
        module=module,
        exclude_sensitive=exclude_sensitive,
        relations_as_ids=relations_as_ids,
    )
    return creator.create_pydantic_model()


def pydantic_queryset_creator(
    model: type[Model],
    *,
    name: str | None = None,
    exclude: tuple[str, ...] | None = None,
    include: tuple[str, ...] | None = None,
    computed: tuple[str, ...] | None = None,
    extra_fields: dict[str, Any] | None = None,
    related_values: dict[str, str] | None = None,
    optional: tuple[str, ...] | None = None,
    allow_cycles: bool | None = None,
    sort_alphabetically: bool | None = None,
    exclude_readonly: bool = False,
    meta_override: type | None = None,
    model_config: ConfigDict | None = None,
    validators: dict[str, Any] | None = None,
    module: str = PYDANTIC_MODELS_MODULE,
    exclude_sensitive: bool = False,
    relations_as_ids: bool = False,
) -> type[PydanticListModel]:
    """Builds a Pydantic model (https://docs.pydantic.dev/latest/concepts/models/) list off a
    Hare model.

    Args:
        model: The Hare Model to put in a list.
        name: A custom name for the list model, instead of ``<item model name>_list``. The item
            model keeps its own default name.
        exclude: Extra fields to exclude from the provided model.
        include: Extra fields to include from the provided model.
        computed: Extra computed fields to include from the provided model.
        extra_fields: Values read from a row by name - a query's annotation, an attribute set on the row
            after it was read - with their types; None where a row has none. A path through the
            relations (``"author.books_count"``) adds one to the related schema.
        related_values: Values of related rows, flattened - a name to the path of the related field
            through relations to one row (``{"author_name": "author.name"}``); None where a relation
            wasn't loaded.
        optional: Extra optional fields of the item model.
        allow_cycles: Whether to allow cycles in the generated model - only useful for
            recursive/self-referential models. ``False`` (the default) prevents any backtracking.
        sort_alphabetically: Sort the parameters alphabetically instead of field-definition
            order. The default order is: field definition order, then order of reverse relations
            (as discovered), then order of computed functions (as provided).
        exclude_readonly: Build the item model without readonly fields, as in
            ``pydantic_model_creator``.
        meta_override: A PydanticMeta class to override the model's values.
        model_config: A custom pydantic config of the item model.
        validators: A dictionary of methods that validate fields of the item model.
        module: The name of the module that the item model belongs to.
        exclude_sensitive: Drop every ``sensitive=True`` field, nested models' included.
        relations_as_ids: Render every forward FK/O2O as its flat ``<name>_id`` field instead of a
            nested submodel, as in ``pydantic_model_creator``.
    """

    submodel = pydantic_model_creator(
        model,
        exclude=exclude,
        include=include,
        computed=computed,
        extra_fields=extra_fields,
        related_values=related_values,
        optional=optional,
        allow_cycles=allow_cycles,
        sort_alphabetically=sort_alphabetically,
        exclude_readonly=exclude_readonly,
        meta_override=meta_override,
        model_config=model_config,
        validators=validators,
        module=module,
        exclude_sensitive=exclude_sensitive,
        relations_as_ids=relations_as_ids,
    )
    list_model_name = name or f"{submodel.__name__}_list"

    queryset_index_key = (model, submodel, list_model_name)
    if (list_model := PydanticModelCreator.QUERYSET_MODEL_INDEX.get(queryset_index_key)) is not None:
        return cast("type[PydanticListModel]", list_model)

    new_list_model = create_model(
        list_model_name,
        __base__=PydanticListModel,
        root=(list[submodel], PydanticField(default_factory=list)),  # type: ignore[valid-type]
    )
    new_list_model.__doc__ = FieldDescriptions.get_clean_docstring(model)
    new_list_model.model_config["title"] = name or f"{submodel.model_config['title']}_list"
    new_list_model.model_config["submodel"] = submodel  # type: ignore[typeddict-unknown-key]

    PydanticModelCreator.QUERYSET_MODEL_INDEX[queryset_index_key] = new_list_model
    return new_list_model
