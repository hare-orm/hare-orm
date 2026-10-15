from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING, Any
from weakref import ReferenceType, WeakSet

from hare.contrib.request_query.declaration.request_query_declaration import RequestQueryDeclaration
from hare.exceptions import ConfigurationError
from hare.models import Model

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.contrib.request_query.request_query import RequestQuery
    from hare.query.lookup_info.lookup_info import LookupInfo
    from hare.query.lookup_info.ordering_info import OrderingInfo


class RequestQueryRegistry:
    """The models a request query class reads, tracked so its declaration and parameters are forgotten
    when one of them changes or leaves the registry - and the refusal of a model already
    unregistered."""

    @staticmethod
    def check_model_registered(query_class: type[RequestQuery[Any]], model: type[Model]) -> None:
        """Refuses a queryset of a model that was unregistered (``Hare.unregister_live_models()``) -
        such a class would query a dead class.

        Args:
            query_class: The request query class.
            model: The model of the class's queryset.

        Raises:
            ConfigurationError: The model was bound once and no longer is.
        """
        # Local import: the request query module imports this module.
        from hare.contrib.request_query.request_query import RequestQuery

        if model in RequestQuery.bound_models and not model._meta.is_bound:
            raise ConfigurationError(
                f"{query_class.__qualname__}.Meta.queryset is a queryset of {model.__name__}, which was "
                "unregistered - build the request query for the model registered in its place"
            )

    @staticmethod
    def track_models(query_class: type[RequestQuery[Any]], models: Iterable[type[Model]]) -> None:
        """Records that the class's parameters or declaration read the models.

        Args:
            query_class: The request query class.
            models: The models.
        """
        # Local import: the request query module imports this module.
        from hare.contrib.request_query.request_query import RequestQuery

        for model in models:
            RequestQuery.classes_by_model.setdefault(model, WeakSet()).add(query_class)

    @staticmethod
    def track_described_models(
        class_reference: ReferenceType[type[RequestQuery[Any]]], description: LookupInfo | OrderingInfo
    ) -> None:
        """Records the models of a description a declaration read after it was built.

        Args:
            class_reference: A weak reference to the request query class.
            description: The description.
        """
        request_query_class = class_reference()
        if request_query_class is not None:
            RequestQueryRegistry.track_models(request_query_class, RequestQueryDeclaration.get_models(description))

    @staticmethod
    def forget_parameters(query_class: type[RequestQuery[Any]]) -> None:
        """Forgets the class's declaration and the parameters ``prepare_parameters()`` added or
        described, so both are built again - the types of the parameters too - on the next use.

        Args:
            query_class: The request query class.
        """
        # Local import: the request query module imports this module.
        from hare.contrib.request_query.request_query import RequestQuery

        RequestQuery.declarations.forget_owner(query_class)
        if query_class not in RequestQuery.prepared_classes:
            return
        RequestQuery.prepared_classes.discard(query_class)
        unprepared_fields = RequestQuery.unprepared_fields.pop(query_class, None)
        if unprepared_fields is not None:
            query_class.__pydantic_fields__.clear()
            query_class.__pydantic_fields__.update(unprepared_fields)
            query_class.model_rebuild(force=True)
