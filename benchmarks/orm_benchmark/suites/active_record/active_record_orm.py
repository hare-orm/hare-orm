from __future__ import annotations

from typing import Any, ClassVar

from orm_benchmark.constants import POOL_MAX_SIZE
from orm_benchmark.definitions.target import Target


class ActiveRecordOrm:
    """What one of hare, tortoise-orm and yara-orm - one shared API shape - provides to the scenarios,
    and which of them it has no way to write.

    Attributes:
        supports_window: A window function (``Window(Rank(), ...)``).
        supports_exists: ``Exists(...)`` of a correlated subquery in ``annotate()``.
        supports_chunked_iteration: Iterating a queryset in chunks of rows (``iterator()``).
    """

    #: The ORM a ``run`` process measures - set before its models module is imported, which defines
    #: the models on this ORM's base class.
    current: ClassVar[ActiveRecordOrm | None] = None

    def __init__(self, target: Target) -> None:
        """
        Args:
            target: A target of the ``active_record`` suite.
        """
        self.target = target
        self.is_hare = target.distribution == "hare-orm"
        if self.is_hare:
            import hare
            import hare.fields
            import hare.query.functions as functions
            from hare.query.expressions import Case, Exists, F, OuterReference, Q, When, Window
            from hare.query.functions.window import Rank
            from hare.transactions.transactions import Transactions

            self.model = hare.Model
            self.init_class = hare.Hare
            self.fields = hare.fields
            self.functions = functions
            self.case, self.when, self.q, self.f = Case, When, Q, F
            self.exists, self.outer_reference = Exists, OuterReference
            self.window, self.rank = Window, Rank
            self.in_transaction = Transactions.atomic
            self.coalesce = functions.Coalesce
            self.supports_window = self.supports_exists = self.supports_chunked_iteration = True
        elif target.distribution == "tortoise-orm":
            import tortoise
            import tortoise.fields
            import tortoise.functions as functions
            from tortoise.expressions import Case, F, Q, When
            from tortoise.transactions import in_transaction

            self.model = tortoise.Model
            self.init_class = tortoise.Tortoise
            self.fields = tortoise.fields
            self.functions = functions
            self.case, self.when, self.q, self.f = Case, When, Q, F
            self.in_transaction = in_transaction
            self.coalesce = functions.Coalesce
            self.supports_window = self.supports_exists = self.supports_chunked_iteration = False
        else:
            import yara_orm

            self.model = yara_orm.Model
            self.init_class = yara_orm.YaraOrm
            self.fields = yara_orm.fields
            self.functions = yara_orm
            self.case, self.when, self.q, self.f = yara_orm.Case, yara_orm.When, yara_orm.Q, yara_orm.F
            self.in_transaction = yara_orm.in_transaction
            self.coalesce = yara_orm.functions.Coalesce
            self.supports_window = self.supports_exists = self.supports_chunked_iteration = False

    def get_queries(self, model: Any) -> Any:
        """Where a model's queries start, as the ORM's documentation writes them: hare's manager
        (``Model.objects``), the model class itself on tortoise-orm and yara-orm.

        Args:
            model: The model.

        Returns:
            The manager or the class.
        """
        return model.objects if self.is_hare else model

    def get_db_url(self, database: Any) -> str:
        """The URL of the run's database - with the shared pool size on PostgreSQL; SQLite runs on each
        ORM's own connection handling of one file.

        Args:
            database: The run's ``PostgresqlDatabase`` or ``SqliteDatabase``.

        Returns:
            The URL.
        """
        key = self.target.key
        if self.target.database == "sqlite":
            path = database.path.as_posix()
            if self.is_hare:
                return f"sqlite+aiosqlite:///{path.lstrip('/')}"
            if key == "tortoise-sqlite":
                return f"sqlite://{path}"
            return f"sqlite:///{path}"
        if key == "hare-rust":
            scheme, pool_parameter = "postgresql", "max_size"
        elif key == "hare-asyncpg":
            scheme, pool_parameter = "postgresql+asyncpg", "max_size"
        elif key == "tortoise":
            scheme, pool_parameter = "postgres", "maxsize"
        else:
            scheme, pool_parameter = "postgres", "max_size"
        return (
            f"{scheme}://postgres:postgres@127.0.0.1:{database.port}/{database.name}?{pool_parameter}={POOL_MAX_SIZE}"
        )
