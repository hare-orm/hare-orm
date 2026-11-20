from __future__ import annotations

from collections.abc import Iterable
from threading import RLock
from typing import TYPE_CHECKING, Any, ClassVar
from weakref import ReferenceType, WeakKeyDictionary, WeakSet

from hare.contrib.request_query.declaration.request_query_declaration import RequestQueryDeclaration
from hare.core.caching.cache import Cache
from hare.models import Model

if TYPE_CHECKING:
    from hare.contrib.request_query.request_query import RequestQuery
    from hare.query.lookup_info.lookup_info import LookupInfo
    from hare.query.lookup_info.ordering_info import OrderingInfo


class RequestQueryRegistry:
    """Track declaration dependencies and invalidate future parameter generations.

    A generation keeps its selected queryset and graph. Explicit invalidation or an extension
    change rebuilds later generations; completed schemas and request instances remain intact.
    """

    lock: ClassVar[RLock] = RLock()
    revision: ClassVar[int] = 0
    #: Completed declarations follow each request query class's lifetime through its bucket.
    declarations: ClassVar[Cache[Any]] = Cache()
    #: Both ends are weak: invalidation must not keep a runtime model or query class alive.
    classes_by_model: ClassVar[WeakKeyDictionary[type[Model], WeakSet[type]]] = WeakKeyDictionary()

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
                    classes = RequestQueryRegistry.classes_by_model[model] = WeakSet()
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
                RequestQueryRegistry.declarations.forget_holder(query_class)

    @staticmethod
    def forget_all() -> None:
        """Invalidate all future declaration reads without changing existing request instances."""
        with RequestQueryRegistry.lock:
            RequestQueryRegistry.revision += 1
            RequestQueryRegistry.declarations.clear()
            RequestQueryRegistry.classes_by_model.clear()
