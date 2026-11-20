from __future__ import annotations

from collections.abc import Callable
from copy import copy
from typing import TYPE_CHECKING, Any, ClassVar

from hare.classes.deferred_import import DeferredImport
from hare.exceptions import ConfigurationError
from hare.query.enums import FilterMethod

if TYPE_CHECKING:
    from hare.ddl.raw_sql_term import RawSQLTerm
    from hare.models import Model
    from hare.models.model_binding import ModelBinding
    from hare.query.filters.resolution.filter_values import FilterValues
    from hare.query.queryset.queryset import QuerySet


class ModelQuery:
    """A queryset of the model whose ``Meta`` declares it, written while that model isn't built yet:
    the calls are recorded and made on the model's queryset once the model is built -
    ``View("paid_invoices", query=ModelQuery().filter(paid=True).values("id", "amount"))``.

    Filter inputs are captured when a call is recorded. Chaining and binding use shallow
    copies, retaining subclasses without repeating their constructors. Custom copy hooks
    must return independent declarations; existing call history is never changed in place.

    Args:
        calls: The recorded calls, ``(method name, args, kwargs)`` each.
        model: The model the calls are made on - set when the model is built.
    """

    #: Filter literals are captured only when a recorded method is a filter.
    filter_values_class: ClassVar[DeferredImport[type[FilterValues]]] = DeferredImport(
        "hare.query.filters.resolution.filter_values", "FilterValues"
    )

    def __init__(
        self,
        calls: tuple[tuple[str, tuple[Any, ...], dict[str, Any]], ...] = (),
        model: type[Model] | None = None,
        *,
        binding: ModelBinding | None = None,
    ) -> None:
        self.calls = tuple(
            (
                name,
                args,
                ModelQuery.filter_values_class.get_captured_filter_kwargs(kwargs) if name in FilterMethod else kwargs,
            )
            for name, args, kwargs in calls
        )
        self.model = model
        self._binding = binding

    def __getattr__(self, name: str) -> Callable[..., ModelQuery]:
        if name.startswith("_"):
            raise AttributeError(name)

        def record(*args: Any, **kwargs: Any) -> ModelQuery:
            recorded = copy(self)
            captured = (
                ModelQuery.filter_values_class.get_captured_filter_kwargs(kwargs) if name in FilterMethod else kwargs
            )
            recorded.calls = (*self.calls, (name, args, captured))
            return recorded

        return record

    def for_model(self, model: type[Model]) -> ModelQuery:
        """The calls, to be made on a model's queryset.

        Args:
            model: The model.
        """
        declared = copy(self)
        declared.model = model
        declared._binding = None
        return declared

    def for_binding(self, binding: ModelBinding) -> ModelQuery:
        """Bind recorded calls to the declaring node without capturing execution resources."""
        target = (
            binding if self.model is None or self.model is binding._model else binding.graph.get_binding(self.model)
        )
        declared = copy(self)
        declared.model = target._model
        declared._binding = target
        return declared

    def get_queryset(self) -> QuerySet[Any, Any]:
        """The model's queryset with the calls made on it.

        Raises:
            ConfigurationError: No model is set, a call isn't a method of the queryset, or the calls
                don't end in a queryset.
        """
        # Imported here: the query package imports the models package.
        from hare.query.queryset.queryset import QuerySet

        if self._binding is None:
            raise ConfigurationError("ModelQuery is a queryset of the model whose Meta declares it - use it there")
        queryset: Any = self._binding.manager.create_queryset(binding=self._binding, context=None)
        for name, args, kwargs in self.calls:
            method = getattr(queryset, name, None)
            if not callable(method):
                raise ConfigurationError(
                    f"ModelQuery of {self._binding._model.__name__}: {name}() isn't a method of its queryset"
                )
            queryset = method(*args, **kwargs)
        if not isinstance(queryset, QuerySet):
            raise ConfigurationError(
                f"ModelQuery of {self._binding._model.__name__}: the calls must end in a queryset, got {queryset!r}"
            )
        return queryset

    @staticmethod
    def get_declared_queryset(
        query: RawSQLTerm | QuerySet[Any, Any] | ModelQuery,
        *,
        binding: ModelBinding | None = None,
    ) -> QuerySet[Any, Any] | None:
        """Select a schema declaration's queryset without selecting execution resources.

        Args:
            query: Raw SQL, recorded model calls or an existing queryset.
            binding: The declaring graph node; a bound queryset keeps its selected origin.

        Returns:
            None for opaque SQL, otherwise the source prepared in its declaring graph.

        Raises:
            ConfigurationError: Recorded model calls have no declaring binding.
        """
        from hare.ddl.raw_sql_term import RawSQLTerm

        if isinstance(query, RawSQLTerm):
            return None
        if isinstance(query, ModelQuery):
            return (query if binding is None else query.for_binding(binding)).get_queryset()
        if binding is not None and query._binding is None:
            from hare.query.queryset.pending_calls.calls_before_setup import CallsBeforeSetup

            return CallsBeforeSetup.bind(query, None, binding.graph)
        return query

    def __repr__(self) -> str:
        calls = "".join(
            f".{name}({', '.join([*map(repr, args), *(f'{key}={value!r}' for key, value in kwargs.items())])})"
            for name, args, kwargs in self.calls
        )
        return f"ModelQuery(){calls}"
