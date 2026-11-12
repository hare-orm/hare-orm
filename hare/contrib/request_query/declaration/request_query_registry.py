from __future__ import annotations

from collections.abc import Iterable
from threading import RLock
from typing import TYPE_CHECKING, Any, ClassVar
from weakref import ReferenceType, WeakKeyDictionary, WeakSet

from hare.contrib.request_query.declaration.request_query_declaration import RequestQueryDeclaration
from hare.core.caching.cache import Cache
from hare.exceptions import ConfigurationError
from hare.models import Model

if TYPE_CHECKING:
    from hare.contrib.request_query.request_query import RequestQuery
    from hare.query.lookup_info.lookup_info import LookupInfo
    from hare.query.lookup_info.ordering_info import OrderingInfo


class RequestQueryRegistry:
    """The models a request query class reads, tracked so its declaration and parameters are forgotten
    when one of them changes or leaves the registry - and the refusal of a model already
    unregistered."""

    lock: ClassVar[RLock] = RLock()
    revision: ClassVar[int] = 0
    #: Completed declarations follow each request query class's lifetime through its bucket.
    declarations: ClassVar[Cache[Any]] = Cache(holds_sql=False)
    #: Both ends are weak: invalidation must not keep a runtime model or query class alive.
    classes_by_model: ClassVar[WeakKeyDictionary[type[Model], WeakSet[type]]] = WeakKeyDictionary()
    bound_models: ClassVar[WeakSet[type[Model]]] = WeakSet()

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
        if model in RequestQueryRegistry.bound_models and not model._meta.is_bound:
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
        with RequestQueryRegistry.lock:
            for model in models:
                classes = RequestQueryRegistry.classes_by_model.get(model)
                if classes is None:
                    classes = RequestQueryRegistry.classes_by_model.setdefault(model, WeakSet())
                classes.add(query_class)

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
        """Forget future construction's generation without mutating returned schemas.

        Parameters, their types and the checked declaration are built together on
        the next use. Existing instances keep their completed generation.

        Args:
            query_class: The request query class.
        """
        with RequestQueryRegistry.lock:
            RequestQueryRegistry.revision += 1
            RequestQueryRegistry.declarations.forget_holder(query_class)

    @staticmethod
    def forget_model(model: type[Model]) -> None:
        """Invalidate declarations that depend on the model, preserving completed generations."""
        with RequestQueryRegistry.lock:
            RequestQueryRegistry.revision += 1
            for query_class in tuple(RequestQueryRegistry.classes_by_model.pop(model, ())):
                RequestQueryRegistry.forget_parameters(query_class)

    @staticmethod
    def forget_all() -> None:
        """Invalidate all future declaration reads without changing existing request instances."""
        with RequestQueryRegistry.lock:
            RequestQueryRegistry.revision += 1
            RequestQueryRegistry.declarations.clear()
            RequestQueryRegistry.classes_by_model.clear()
