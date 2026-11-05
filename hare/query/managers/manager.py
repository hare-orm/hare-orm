from __future__ import annotations

from copy import copy
from typing import TYPE_CHECKING, Any, ClassVar, Generic, cast, overload

from typing_extensions import TypeVar

from hare.core.caching.cache import Cache
from hare.query.queryset import QuerySet
from hare.query.scopes.row_visibility import RowVisibility

if TYPE_CHECKING:
    from hare.models import Model
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
    unchanged_querysets: ClassVar[Cache[Any]] = Cache(holds_sql=False, depends_on_other_models=True)

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
        queryset = Manager.unchanged_querysets.get_value(self)
        if queryset is not None:
            return queryset
        return self._get_unchanged_queryset()

    def _get_unchanged_queryset(self) -> QuerySet[Any, Any]:
        """The queryset reading the manager off its model gives. Every queryset method returns a
        changed copy, so one queryset serves every read - unless ``get_queryset()`` is overridden
        (it may answer differently each time) or the model isn't set up yet. A change to the
        models makes it anew.

        Returns:
            The queryset.
        """
        queryset = self.create_queryset()
        model = cast("type[Model]", self._model)
        if not self.overrides_get_queryset:
            if queryset._visibility is RowVisibility.DEFAULT and self.scope is None:
                # Its queries run by the calls made on it (QuerySet._call_signature) - also one taken
                # before the model is set up and kept, as `Model.objects` read once at import.
                queryset._call_signature = (type(queryset),)
            if model._meta._inited:
                Manager.unchanged_querysets.set_value(self, queryset)
        return queryset

    def get_queryset(self) -> QuerySet[Any, Any]:
        """A new queryset of the model's rows - the starting point of every query made through
        the manager. Override it to configure the queryset factory; use scope for predicates
        that must also govern relation traversal.

        Returns:
            The queryset.
        """
        # _model is None only transiently, between Manager() construction and the model class
        # assignment that immediately follows it (see Model.__new__/MetaInfo setup) - never at a
        # real call site, since query methods only run through a fully-constructed Model class.
        model: type[Model] = self._model  # type: ignore[assignment]
        queryset = self.queryset_class(model)
        queryset._uses_default_scope = True
        queryset._row_scope_provider = self.scope
        return queryset

    def create_queryset(self) -> QuerySet[Any, Any]:
        """Attaches explicit row policies to the custom factory's declaration."""
        queryset = self.get_queryset()
        queryset._uses_default_scope = True
        queryset._row_scope_provider = self.scope
        return queryset

    def copy_unbound(self) -> Manager[TQuerySet]:
        """Builds a copy of this manager, keeping its constructor state, for binding to another model.

        Returns:
            A new manager of the same class with the same instance attributes.
        """
        manager_copy = copy(self)
        manager_copy.cache_buckets = {}
        return manager_copy
