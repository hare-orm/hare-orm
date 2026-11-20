from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Mapping

    from hare.core.apps import Apps
    from hare.dialects.base.dialect import Dialect
    from hare.models.model_binding import ModelBinding


class BoundModels:
    """Prepared models and their graph dialects, read without connecting to a database -
    what the typing tools (the mypy plugin, ``hare stubs``) read the models through.

    Args:
        apps: The prepared registry providing model bindings and dialects.
    """

    def __init__(self, apps: Apps) -> None:
        models = sorted(apps.get_models_iterable(), key=BoundModels.get_fullname)
        self.bindings: list[ModelBinding] = [apps.get_binding(model) for model in models]
        #: Dialects captured by the same graph as the described bindings.
        self.dialects_by_connection: Mapping[str, Dialect] = apps.binding_graph.dialects

    def get_dialect(self, binding: ModelBinding) -> Dialect | None:
        """The dialect of the connection a model's queries run on.

        Args:
            binding: The model.

        Returns:
            The dialect, None when the model has no connection of the configuration.
        """
        connection_alias = binding.default_connection
        return None if connection_alias is None else self.dialects_by_connection.get(connection_alias)

    @staticmethod
    def get_fullname(named_class: type[Any]) -> str:
        """The full name of a class the way the type checkers name it.

        Args:
            named_class: The class.

        Returns:
            ``module.QualifiedName``.
        """
        return f"{named_class.__module__}.{named_class.__qualname__}"
