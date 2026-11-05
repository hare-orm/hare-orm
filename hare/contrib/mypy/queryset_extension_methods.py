from __future__ import annotations

import inspect
from typing import TYPE_CHECKING

from mypy.nodes import Argument, Var
from mypy.plugins.common import add_method_to_class
from mypy.types import AnyType, TypeOfAny
from mypy.typevars import fill_typevars

from hare.contrib.mypy.constants import ARGUMENT_TYPES, QUERYSET_FULLNAME
from hare.query.queryset.extensions.query_set_extensions import QuerySetExtensions

if TYPE_CHECKING:
    from mypy.plugin import ClassDefContext

    from hare.contrib.mypy.model_registry import ModelRegistry
    from hare.query.queryset.extensions.query_set_extension import QuerySetExtension


class QuerySetExtensionMethods:
    """Declares on mypy's ``QuerySet`` the methods the dialects of the project's connections
    registered (``QuerySetExtensions``) - each with the parameters of its call, of any type, returning
    the queryset; a name no such dialect registered stays unknown to mypy.

    Args:
        model_registry: The project's models and connections.
    """

    def __init__(self, model_registry: ModelRegistry) -> None:
        self.model_registry = model_registry

    def add_methods(self, class_definition_context: ClassDefContext) -> None:
        """Adds the methods to ``QuerySet`` as mypy reads its class.

        Args:
            class_definition_context: The class mypy reads - any subclass of ``QuerySpecification``; only ``QuerySet``
                gets them.
        """
        if class_definition_context.cls.fullname != QUERYSET_FULLNAME:
            return
        self.model_registry.load()
        bound_models = self.model_registry.bound_models
        if self.model_registry.load_error is not None or bound_models is None:
            return
        dialect_classes = [type(dialect) for _alias, dialect in sorted(bound_models.dialects_by_connection.items())]
        any_type = AnyType(TypeOfAny.explicit)
        for name, implementations in sorted(QuerySetExtensions.get_registered().items()):
            implementation = next(
                (
                    implementations[dialect_class]
                    for dialect_class in dialect_classes
                    if dialect_class in implementations
                ),
                None,
            )
            if implementation is None:
                continue
            add_method_to_class(
                class_definition_context.api,
                class_definition_context.cls,
                name,
                self.get_arguments(implementation, any_type),
                return_type=fill_typevars(class_definition_context.cls.info),
            )

    @staticmethod
    def get_arguments(implementation: QuerySetExtension, any_type: AnyType) -> list[Argument]:
        """mypy's arguments of a method's call.

        Args:
            implementation: The method's implementation.
            any_type: The type of every argument.

        Returns:
            The arguments.
        """
        arguments = []
        for parameter in implementation.get_call_signature().parameters.values():
            required_type, optional_type = ARGUMENT_TYPES[parameter.kind]
            arguments.append(
                Argument(
                    Var(parameter.name, any_type),
                    any_type,
                    None,
                    required_type if parameter.default is inspect.Parameter.empty else optional_type,
                    pos_only=parameter.kind is inspect.Parameter.POSITIONAL_ONLY,
                )
            )
        return arguments
