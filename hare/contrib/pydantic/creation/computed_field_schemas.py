from __future__ import annotations

import functools
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from pydantic import computed_field

from hare.contrib.pydantic.creation.field_descriptions import FieldDescriptions
from hare.contrib.pydantic.creation.model_annotations import ModelAnnotations
from hare.contrib.pydantic.descriptions.computed_field_description import ComputedFieldDescription
from hare.exceptions import NoValuesFetched

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.contrib.pydantic.creation.pydantic_model_creator import PydanticModelCreator


class ComputedFieldSchemas:
    """The fields a model's properties and methods named in PydanticMeta.computed get in the pydantic
    model, typed by their return annotations."""

    @staticmethod
    def process_computed_field(
        creator: PydanticModelCreator,
        field: ComputedFieldDescription,
    ) -> Any:
        function: Callable[..., Any] | None
        if isinstance(field.function, property):
            function = field.function.fget
        elif isinstance(field.function, functools.cached_property):
            function = field.function.func
        else:
            function = field.function
        if function is None:
            return None
        annotation = ModelAnnotations.get(creator._model_class, function).get("return", None)
        if annotation is not None:
            original_function = function

            @functools.wraps(original_function)
            def wrapped_function(self_pydantic: Any) -> Any:
                # __orm_obj__ is set only when validated from an ORM instance; from a plain payload
                # the computed function runs on the pydantic instance.
                orm_obj = getattr(self_pydantic, "__orm_obj__", None)
                if orm_obj is not None:
                    try:
                        return original_function(orm_obj)
                    except NoValuesFetched:
                        raise NoValuesFetched(
                            f"Computed field '{original_function.__name__}' tried to access a "
                            f"relation that has not been fetched. Either include the relation "
                            f"in the Pydantic model so it is auto-prefetched, or call "
                            f"prefetch_related_objects() before serialization."
                        )
                return original_function(self_pydantic)

            comment = FieldDescriptions.get_clean_docstring(function)
            c_f = computed_field(return_type=annotation, description=comment)
            return c_f(wrapped_function)
        return None

    @staticmethod
    def process_computed_field_entry(
        creator: PydanticModelCreator, field_name: str, field: ComputedFieldDescription
    ) -> None:
        field_property = ComputedFieldSchemas.process_computed_field(creator, field)
        if field_property:
            creator._properties[field_name] = field_property
