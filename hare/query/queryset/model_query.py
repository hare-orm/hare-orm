from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from hare.exceptions import ConfigurationError

if TYPE_CHECKING:
    from hare.models import Model
    from hare.query.queryset.queryset import QuerySet


class ModelQuery:
    """A queryset of the model whose ``Meta`` declares it, written while that model isn't built yet:
    the calls are recorded and made on the model's queryset once the model is built -
    ``View("paid_invoices", query=ModelQuery().filter(paid=True).values("id", "amount"))``.

    Args:
        calls: The recorded calls, ``(method name, args, kwargs)`` each.
        model: The model the calls are made on - set when the model is built.
    """

    def __init__(
        self,
        calls: tuple[tuple[str, tuple[Any, ...], dict[str, Any]], ...] = (),
        model: type[Model] | None = None,
    ) -> None:
        self.calls = calls
        self.model = model

    def __getattr__(self, name: str) -> Callable[..., ModelQuery]:
        if name.startswith("_"):
            raise AttributeError(name)

        def record(*args: Any, **kwargs: Any) -> ModelQuery:
            return ModelQuery((*self.calls, (name, args, kwargs)), self.model)

        return record

    def for_model(self, model: type[Model]) -> ModelQuery:
        """The calls, to be made on a model's queryset.

        Args:
            model: The model.
        """
        return ModelQuery(self.calls, model)

    def get_queryset(self) -> QuerySet[Any, Any]:
        """The model's queryset with the calls made on it.

        Raises:
            ConfigurationError: No model is set, a call isn't a method of the queryset, or the calls
                don't end in a queryset.
        """
        # Imported here: the query package imports the models package.
        from hare.query.queryset.queryset import QuerySet

        if self.model is None:
            raise ConfigurationError("ModelQuery is a queryset of the model whose Meta declares it - use it there")
        queryset: Any = self.model._meta.manager.get_queryset()
        for name, args, kwargs in self.calls:
            method = getattr(queryset, name, None)
            if not callable(method):
                raise ConfigurationError(
                    f"ModelQuery of {self.model.__name__}: {name}() isn't a method of its queryset"
                )
            queryset = method(*args, **kwargs)
        if not isinstance(queryset, QuerySet):
            raise ConfigurationError(
                f"ModelQuery of {self.model.__name__}: the calls must end in a queryset, got {queryset!r}"
            )
        return queryset

    def __repr__(self) -> str:
        calls = "".join(
            f".{name}({', '.join([*map(repr, args), *(f'{key}={value!r}' for key, value in kwargs.items())])})"
            for name, args, kwargs in self.calls
        )
        return f"ModelQuery(){calls}"
