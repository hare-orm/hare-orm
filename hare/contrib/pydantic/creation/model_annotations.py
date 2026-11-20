from __future__ import annotations

import builtins
import inspect
import sys
from collections.abc import Callable, Collection
from typing import TYPE_CHECKING, Any, get_type_hints

if TYPE_CHECKING:
    from hare.models import Model
    from hare.models.model_binding import ModelBinding

try:
    import annotationlib
except ImportError:
    annotationlib = None


class ModelAnnotations:
    """The type annotations of a model class and its methods."""

    @staticmethod
    def get(
        model: type[Model], method: Callable[..., Any] | None = None, *, binding: ModelBinding | None = None
    ) -> dict[str, Any]:
        """Gets all annotations including base classes.

        Each declaring class uses its module and class namespaces. Registered models
        supplement missing names without replacing lexical types or builtins. Methods
        use the original function's namespace, including when wrapped by decorators.

        Args:
            model: The model whose annotations are read.
            method: If specified, we try to get the annotations for the callable.
            binding: The structural graph supplying missing model names. Without it only
                lexical namespaces and builtins participate.

        Returns:
            The annotations dict.
        """
        localns = ModelAnnotations.get_registry_namespace(binding)
        subject = model if method is None else method
        try:
            if getattr(subject, "__no_type_check__", False):
                return {}
            return (
                ModelAnnotations.get_class_annotations(
                    model, localns, field_names=None if binding is None else binding.fields_map
                )
                if method is None
                else ModelAnnotations.get_function_annotations(method, localns)
            )
        except NameError:
            # Unknown names remain errors instead of silently becoming raw strings.
            raise
        except Exception:
            return getattr(subject, "__annotations__", {})

    @staticmethod
    def get_registry_namespace(binding: ModelBinding | None) -> dict[str, Any]:
        """Read forward-reference fallback names from the explicitly selected graph."""
        return {} if binding is None else {model.__name__: model for model in binding.graph.bindings}

    @staticmethod
    def get_declared_annotations(subject: type[Any] | Callable[..., Any]) -> dict[str, Any]:
        """Read declarations before deferred annotation evaluation requires missing names."""
        if annotationlib is not None:
            return annotationlib.get_annotations(subject, format=annotationlib.Format.FORWARDREF)
        return inspect.get_annotations(subject, eval_str=False)

    @staticmethod
    def get_builtin_namespace(module_globals: dict[str, Any]) -> dict[str, Any]:
        """Use the declaring module's builtins before consulting registered models."""
        namespace = module_globals.get("__builtins__", builtins)
        return namespace if isinstance(namespace, dict) else vars(namespace)

    @staticmethod
    def get_class_annotations(
        model: type[Any], registry: dict[str, Any], *, field_names: Collection[str] | None = None
    ) -> dict[str, Any]:
        """Evaluate each owner in its namespace; a model schema reads only declared field types.

        Internal instance annotations may depend on type-checking-only imports and do not
        describe schema fields. General annotation inspection retains every declaration.
        """
        result = {}
        for owner in reversed(model.__mro__):
            annotations = ModelAnnotations.get_declared_annotations(owner)
            if field_names is not None:
                annotations = {name: value for name, value in annotations.items() if name in field_names}
            if not annotations:
                continue
            module = sys.modules.get(owner.__module__)
            module_globals = vars(module) if module is not None else {}
            fallback = {
                **registry,
                **ModelAnnotations.get_builtin_namespace(module_globals),
                **vars(owner),
            }
            for parameter in getattr(owner, "__type_params__", ()):
                fallback[parameter.__name__] = parameter
            # A plain class lets get_type_hints handle ForwardRef/Union/Annotated without
            # evaluating inherited declarations in a different owner's namespace.
            carrier = type("AnnotationOwner", (), {"__module__": owner.__module__, "__annotations__": annotations})
            if hasattr(owner, "__type_params__"):
                carrier.__type_params__ = owner.__type_params__
            result.update(get_type_hints(carrier, globalns=fallback, localns=module_globals))
        return result

    @staticmethod
    def get_function_annotations(function: Callable[..., Any], registry: dict[str, Any]) -> dict[str, Any]:
        """Evaluate callable declarations in the unwrapped function's original module."""
        module_globals = getattr(inspect.unwrap(function), "__globals__", {})
        namespace = {**registry, **ModelAnnotations.get_builtin_namespace(module_globals), **module_globals}

        def carrier() -> None:
            pass

        carrier.__annotations__ = ModelAnnotations.get_declared_annotations(function)
        if hasattr(function, "__type_params__"):
            carrier.__type_params__ = function.__type_params__
        return get_type_hints(carrier, globalns=namespace, localns=namespace)
