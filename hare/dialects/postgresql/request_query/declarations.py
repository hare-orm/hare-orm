from __future__ import annotations

from typing import ClassVar

from hare.contrib.request_query.request_query import RequestQuery
from hare.dialects.base.dialect import Dialect
from hare.dialects.postgresql.constants import POSTGRESQL_DIALECT
from hare.models import Model


class PostgresqlRequestQuery[ModelType: Model](RequestQuery[ModelType]):
    """A request query of a model on a PostgreSQL connection: its filters and search are checked
    against PostgreSQL only, so they may use what only PostgreSQL runs - full-text ``search``, the
    ``trigram_*`` lookups, array, range and JSON container lookups. The model's connection must be
    a PostgreSQL one."""

    dialect: ClassVar[Dialect | None] = POSTGRESQL_DIALECT
