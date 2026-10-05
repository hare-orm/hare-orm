from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING, Annotated, Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pydantic import Field

from hare.contrib.request_query.enums import ParameterType
from hare.contrib.request_query.options.parameter_field import ParameterField
from hare.contrib.request_query.options.request_option import RequestOption
from hare.contrib.request_query.paginations.constants import (
    DEFAULT_LIMIT_PARAMETER,
    DEFAULT_MAX_PAGE_LIMIT,
    DEFAULT_PAGE_LIMIT,
    MAX_PAGE_LIMIT_CEILING,
)
from hare.exceptions import ConfigurationError

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.contrib.request_query.request_query import RequestQuery
    from hare.models import Model
    from hare.query.queryset import QuerySet


@dataclasses.dataclass(frozen=True, slots=True)
class Pagination(RequestOption):
    """What every pagination has: the page size a request may ask for and the parameter it asks
    with. A pagination adds how a request says which page, and builds the page.

    Args:
        default_limit: The page size of a request that names none.
        max_limit: The largest page size a request may ask for.
        limit_parameter: The name of the page size parameter.

    Raises:
        ConfigurationError: A size isn't an int or isn't in ``1 <= default_limit <= max_limit <=
            MAX_PAGE_LIMIT_CEILING``, or a parameter name isn't an identifier or repeats another.
    """

    default_limit: int = DEFAULT_PAGE_LIMIT
    max_limit: int = DEFAULT_MAX_PAGE_LIMIT
    limit_parameter: str = DEFAULT_LIMIT_PARAMETER

    def __post_init__(self) -> None:
        option_name = type(self).__name__
        for name, value in (("default_limit", self.default_limit), ("max_limit", self.max_limit)):
            if isinstance(value, bool) or not isinstance(value, int):
                raise ConfigurationError(f"{option_name}.{name} must be an int, got {value!r}")
        if not 1 <= self.default_limit <= self.max_limit <= MAX_PAGE_LIMIT_CEILING:
            raise ConfigurationError(
                f"{option_name} needs 1 <= default_limit <= max_limit <= {MAX_PAGE_LIMIT_CEILING}, "
                f"got default_limit={self.default_limit}, max_limit={self.max_limit}"
            )
        parameters = self.get_parameters()
        for parameter in parameters:
            if not isinstance(parameter, str) or not parameter.isidentifier():
                raise ConfigurationError(f"{option_name} parameter names must be identifiers, got {parameter!r}")
        if len(set(parameters)) != len(parameters):
            raise ConfigurationError(f"{option_name} parameter names must differ, got {parameters!r}")

    def get_parameter_fields(self) -> tuple[ParameterField, ...]:
        return (
            ParameterField(
                self.limit_parameter,
                Annotated[int, Field(ge=1, le=self.max_limit)],
                self.default_limit,
                ParameterType.LIMIT,
            ),
        )

    def check_model(self, owner: str, model: type[Model]) -> None:
        """Checks that the pagination can page the model's rows - any model by default.

        Args:
            owner: The request query class, for the error.
            model: The model.
        """

    def check_ordering(self, owner: str, name: str, queryset: QuerySet[Any]) -> None:
        """Checks that the pagination can page by an ordering the class declares - any by default.

        Args:
            owner: The request query class, for the error.
            name: The ordering name, without its direction.
            queryset: The class's queryset.
        """

    @staticmethod
    def get_url_with(request_query: RequestQuery[Any], replacements: dict[str, str | None]) -> str | None:
        """The request's address with some parameters replaced - a link to another page. The other
        parameters, repeated ones included, are kept as they were.

        Args:
            request_query: The request query.
            replacements: The new value of each parameter, None to drop it.

        Returns:
            The address, None without a request or a request address.
        """
        url = request_query.get_request_url()
        if url is None:
            return None
        parts = urlsplit(url)
        pairs = [
            (name, value) for name, value in parse_qsl(parts.query, keep_blank_values=True) if name not in replacements
        ]
        pairs.extend((name, value) for name, value in replacements.items() if value is not None)
        return urlunsplit(parts._replace(query=urlencode(pairs)))

    async def get_page(self, request_query: RequestQuery[Any]) -> Any:
        """The page of rows a request asks for.

        Args:
            request_query: The request query.

        Returns:
            The page.
        """
        raise NotImplementedError
