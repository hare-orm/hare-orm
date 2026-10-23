from __future__ import annotations

from typing import ClassVar

from hare.contrib.request_query.request_query import RequestQuery
from hare.dialects.base.dialect import Dialect
from hare.dialects.sqlite.constants import SQLITE_DIALECT
from hare.models import Model


class SqliteRequestQuery[ModelType: Model](RequestQuery[ModelType]):
    """A request query of a model on a SQLite connection: its filters and search are checked
    against SQLite only, so they may use what SQLite runs and other dialects don't. The model's
    connection must be a SQLite one."""

    dialect: ClassVar[Dialect | None] = SQLITE_DIALECT
