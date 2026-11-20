from __future__ import annotations

from copy import copy
from typing import TYPE_CHECKING, Any, ClassVar, Generic, overload

from typing_extensions import TypeVar

from hare.core.caching.cache import Cache
from hare.query.queryset import QuerySet
from hare.query.scopes.row_visibility import RowVisibility

if TYPE_CHECKING:
    from hare.core.hare_context import HareContext
    from hare.models import Model
    from hare.models.model_binding import ModelBinding
    from hare.query.scopes.row_scope_provider import RowScopeProvider
from hare.query.managers.base_manager import BaseManager

TModel = TypeVar("TModel", bound="Model")
TQuerySet = TypeVar("TQuerySet", default=None)
TCustomQuerySet = TypeVar("TCustomQuerySet", bound="QuerySet[Any, Any]")


class Manager(BaseManager, Generic[TQuerySet]):
    """Where the queries of a model start: ``Model.objects`` - or any other ``Manager`` declared
    on the model class - gives a queryset of the model's rows under its default scopes
    (``Meta.soft_delete_field``, ``Meta.tenant_field``). It isn't reachable from an instance.

    Methods of one's own go on a ``QuerySet`` subclass (``objects = Manager(PublishedQuerySet)``);
    rows every query of the model leaves out, into a RowScopeProvider passed as scope.

    Args:
        queryset_class: The class of the querysets; QuerySet itself when not given.
        scope: The provider of a visibility predicate, prepared separately for each execution.
    """

    #: The queryset reading each manager off its model gives, kept in its cache holder bucket;
    #: a change to any model invalidates it.
    unchanged_querysets: ClassVar[Cache[Any]] = Cache(depends_on_other_models=True)

    @overload
    def __init__(self: Manager[None], *, scope: RowScopeProvider | None = None) -> None: ...

    @overload
    def __init__(
        self: Manager[TCustomQuerySet], queryset_class: type[TCustomQuerySet], *, scope: RowScopeProvider | None = None
    ) -> None: ...

    def __init__(
        self, queryset_class: type[QuerySet[Any, Any]] | None = None, *, scope: RowScopeProvider | None = None
    ) -> None:
        self.scope = scope
        self.queryset_class: type[QuerySet[Any, Any]] = QuerySet if queryset_class is None else queryset_class
        self._model: type[Model] | None = None
        #: Whether ``get_queryset()`` is a subclass's own - it may then leave rows out, and
        #: answer differently each time.
        self.overrides_get_queryset = type(self).get_queryset is not Manager.get_queryset
        #: The manager's value in ``unchanged_querysets``, None while it has none.
        self.cache_buckets: dict[int, Any] = {}

    @overload  # type: ignore[override]
    def __get__(self: Manager[None], obj: None, owner: type[TModel]) -> QuerySet[TModel, TModel]: ...

    @overload
    def __get__(self: Manager[TCustomQuerySet], obj: None, owner: type[Any]) -> TCustomQuerySet: ...

    def __get__(self, obj: Any, owner: Any) -> Any:
        if obj is not None:
            raise AttributeError(f"A manager isn't reachable from an instance of {owner.__name__} - use the class")
        from hare.core.hare_context import HareContext

        context = HareContext.get_current()
        if context is None or (apps := context.apps) is None:
            return self.create_queryset()
        binding = apps.get_prepared_binding(owner)
        if binding is None:
            return self.create_queryset()
        return self.get_bound_queryset(binding, context)

    def get_bound_queryset(self, binding: ModelBinding, context: HareContext) -> QuerySet[Any, Any]:
        """The manager declaration for this exact graph and origin; custom factories run each time."""
        registry = context.apps
        if (
            registry is None
            or binding.graph is not registry.binding_graph
            or not binding._inited
            or self.overrides_get_queryset
        ):
            return self.create_queryset(binding=binding, context=context)
        bucket = Manager.unchanged_querysets.get_bucket(registry)
        key = (self, binding)
        queryset = bucket.get(key)
        if queryset is None:
            queryset = self.create_queryset(binding=binding, context=context)
            queryset._unchanged_base_manager = self
            bucket[key] = queryset
        return queryset

    def get_queryset(
        self, *, binding: ModelBinding | None = None, context: HareContext | None = None
    ) -> QuerySet[Any, Any]:
        """A new queryset of the model's rows - the starting point of every query made through
        the manager. Override it to configure the queryset factory; use scope for predicates
        that must also govern relation traversal.

        Returns:
            The queryset.
        """
        # _model is None only transiently, between Manager() construction and the model class
        # assignment that immediately follows it (see Model.__new__/ModelBinding setup) - never at a
        # real call site, since query methods only run through a fully-constructed Model class.
        model: type[Model] = self._model  # type: ignore[assignment]
        queryset = self.queryset_class(model, binding=binding, context=context)
        queryset._uses_default_scope = True
        queryset._row_scope_provider = self.scope
        return queryset

    def create_queryset(
        self, *, binding: ModelBinding | None = None, context: HareContext | None = None
    ) -> QuerySet[Any, Any]:
        """Attach row policies and the call signature of an unchanged factory's declaration.

        The same initialization serves manager access and relation loading. An overridden
        factory or a scope provider retains its ordinary query-building path.
        """
        queryset = self.get_queryset(binding=binding, context=context)
        queryset._uses_default_scope = True
        queryset._row_scope_provider = self.scope
        if not self.overrides_get_queryset and queryset._visibility is RowVisibility.DEFAULT and self.scope is None:
            queryset._call_signature = (type(queryset),)
        return queryset

    def copy_unbound(self) -> Manager[TQuerySet]:
        """Builds a copy of this manager, keeping its constructor state, for binding to another model.

        Returns:
            A new manager of the same class with the same instance attributes.
        """
        manager_copy = copy(self)
        manager_copy.cache_buckets = {}
        return manager_copy
