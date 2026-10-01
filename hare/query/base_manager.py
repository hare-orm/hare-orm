from __future__ import annotations

from typing import TYPE_CHECKING

from typing_extensions import TypeVar

from hare.query.queryset import QuerySet

if TYPE_CHECKING:
    from hare.models import Model

TModel = TypeVar("TModel", bound="Model")


class BaseManager:
    """What ``Model.objects`` is declared as: read off a model class, it gives a queryset of that
    model's rows."""

    def __get__(self, instance: None, owner: type[TModel]) -> QuerySet[TModel]:
        raise NotImplementedError()  # pragma: nocoverage
