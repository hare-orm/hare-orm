from __future__ import annotations

import dataclasses
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from pydantic import Field

from hare.contrib.request_query.constants import (
    DEFAULT_DELETED_PARAMETER,
)
from hare.contrib.request_query.enums import DeletedRows, ParameterType
from hare.exceptions import ConfigurationError

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.models import Model
    from hare.query.queryset import QuerySet
from hare.contrib.request_query.options.parameter_field import ParameterField
from hare.contrib.request_query.options.request_option import RequestOption


@dataclasses.dataclass(frozen=True, slots=True)
class DeletedConfig(RequestOption):
    """Lets a request see the soft-deleted rows of a model with ``Meta.soft_delete_field``:
    ``?deleted=include`` - every row, ``?deleted=only`` - the deleted ones, ``?deleted=exclude`` or
    no value - the rest, as by default. Asking for deleted rows needs the query's
    ``may_see_deleted()`` to allow it.

    Args:
        parameter: The name of the parameter.
    """

    parameter: str = DEFAULT_DELETED_PARAMETER

    def __post_init__(self) -> None:
        self.check_parameter_name(type(self).__name__, self.parameter)

    def get_parameter_fields(self) -> tuple[ParameterField, ...]:
        description = "Deleted rows: exclude (by default), include or only"
        return (
            ParameterField(
                self.parameter,
                DeletedRows | None,
                Field(default=None, description=description),
                ParameterType.DELETED,
                tuple(DeletedRows),
            ),
        )

    def get_requested(self, values: Mapping[str, Any]) -> DeletedRows:
        """Which rows a request asks for.

        Args:
            values: The request query's values by parameter.

        Returns:
            ``EXCLUDE`` when the request names none.
        """
        requested = values.get(self.parameter)
        return requested if isinstance(requested, DeletedRows) else DeletedRows.EXCLUDE

    def apply(self, queryset: QuerySet[Any], deleted_rows: DeletedRows) -> QuerySet[Any]:
        """Includes or selects the deleted rows.

        Args:
            queryset: A queryset of the model.
            deleted_rows: Which rows.

        Returns:
            The queryset.
        """
        if deleted_rows is DeletedRows.INCLUDE:
            return queryset.include_deleted()
        if deleted_rows is DeletedRows.ONLY:
            return queryset.only_deleted()
        return queryset

    def check_model(self, owner: str, model: type[Model]) -> None:
        """Checks that the model has deleted rows at all.

        Args:
            owner: The request query class, for the error.
            model: The model.

        Raises:
            ConfigurationError: The model has no ``Meta.soft_delete_field``.
        """
        if model._meta.soft_delete_field is None:
            raise ConfigurationError(
                f"{owner}.Meta.deleted: {model.__name__} has no Meta.soft_delete_field - "
                "it has no deleted rows to show"
            )
