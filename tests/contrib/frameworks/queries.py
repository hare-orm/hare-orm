"""Request queries every framework's tests share."""

from typing import Annotated, Self

from pydantic import model_validator

from hare.contrib.request_query import CommaSeparated, Filter, OrderingConfig, RequestQuery
from tests.contrib.frameworks.models import Writer


class NamedWriterQuery(RequestQuery[Writer]):
    """A query refusing a combination of its parameters, not one parameter."""

    name: str | None = None
    name__in: list[str] | None = None
    ids: Annotated[CommaSeparated[int] | None, Filter("id", lookup="in")] = None

    class Meta:
        queryset = Writer.objects.all()
        ordering = OrderingConfig(default=("name",))
        pagination = None

    @model_validator(mode="after")
    def check_one_name_filter(self) -> Self:
        if self.name is not None and self.name__in is not None:
            raise ValueError("Give name or name__in, not both")
        return self
