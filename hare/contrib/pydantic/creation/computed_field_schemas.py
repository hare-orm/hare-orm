from __future__ import annotations

import functools
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from pydantic import computed_field

from hare.contrib.pydantic.creation.field_descriptions import FieldDescriptions
from hare.contrib.pydantic.creation.model_annotations import ModelAnnotations
from hare.contrib.pydantic.descriptions.computed_field_description import ComputedFieldDescription
from hare.contrib.pydantic.models.pydantic_model import PydanticModel
from hare.exceptions import NoValuesFetched

if TYPE_CHECKING:
    from hare.contrib.pydantic.creation.pydantic_model_creator import PydanticModelCreator


class ComputedFieldSchemas:
    """The fields a model's properties and methods named in Meta.pydantic.computed get in the pydantic
    model, typed by their return annotations."""

    @staticmethod
    def process_computed_field(
        creator: PydanticModelCreator,
        field_name: str,
        field: ComputedFieldDescription,
    ) -> Any:
        """The Pydantic computed field of a model's property or method - typed by its return annotation.

        Args:
            creator: The creator of the schema.
            field_name: The attribute selected from the model.
            field: The computed field.

        Returns:
            The computed field, None for a property without a getter.
        """
        function: Callable[..., Any] | None
        if isinstance(field.function, property):
            function = field.function.fget
        elif isinstance(field.function, functools.cached_property):
            function = field.function.func
        else:
            function = field.function
        if function is None:
            return None
        annotation = ModelAnnotations.get(creator._binding._model, function, binding=creator._binding).get(
            "return", None
        )
        if annotation is not None:
            original_function = function
            is_property = isinstance(field.function, (property, functools.cached_property))

            @functools.wraps(original_function)
            def wrapped_function(self_pydantic: Any) -> Any:
                # The ORM source is available during validators as well as after validation;
                # a plain payload computes from the Pydantic instance instead.
                orm_obj = PydanticModel._get_orm_source(self_pydantic)
                if orm_obj is not None:
                    try:
                        value = getattr(orm_obj, field_name)
                        return value if is_property else value()
                    except NoValuesFetched:
                        raise NoValuesFetched(
                            f"Computed field {original_function.__name__!r} tried to access a "
                            f"relation that has not been fetched. Either include the relation "
                            f"in the Pydantic model so it is auto-prefetched, or call "
                            f"prefetch_related_objects() before serialization."
                        )
                return original_function(self_pydantic)

            comment = FieldDescriptions.get_clean_docstring(function)
            c_f = computed_field(return_type=annotation, description=comment)
            if isinstance(field.function, functools.cached_property):
                return c_f(functools.cached_property(wrapped_function))
            return c_f(wrapped_function)
        return None

    @staticmethod
    def process_computed_field_entry(
        creator: PydanticModelCreator, field_name: str, field: ComputedFieldDescription
    ) -> None:
        field_property = ComputedFieldSchemas.process_computed_field(creator, field_name, field)
        if field_property:
            creator._properties[field_name] = field_property
