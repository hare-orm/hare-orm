from __future__ import annotations

from collections.abc import Iterable, Mapping
from copy import deepcopy
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Self

from hare.classes.class_path import ClassPath
from hare.ddl.expressions.declared_expressions import DeclaredExpressions
from hare.ddl.expressions.expression_field_renaming import ExpressionFieldRenaming
from hare.ddl.raw_sql_term import RawSQLTerm
from hare.dialects.clickhouse.cluster.constants import CLICKHOUSE_UUID_MACRO
from hare.dialects.clickhouse.query.constants import CLICKHOUSE_ENGINE_PREFIXES, CLICKHOUSE_FINAL_ENGINES
from hare.dialects.clickhouse.schema.messages import (
    ENGINE_INVALID_NAME,
    ENGINE_MISSING_REPLICA_DEFAULTS,
    ENGINE_RAW_SHADOW_PATH,
    ENGINE_REPLICA_ARGUMENTS,
    ENGINE_UNSCOPED_REPLICA_DEFAULT,
)
from hare.exceptions import ConfigurationError, UnSupportedError
from hare.query.expressions import Expression
from hare.query.expressions.structure.expression_traversal import ExpressionTraversal

if TYPE_CHECKING:
    from hare.dialects.base.dialect import Dialect
    from hare.models import Model


@dataclass(frozen=True, init=False)
class ClickhouseEngine:
    """A table engine with explicit expression, literal and replication arguments.

    ``ClickhouseEngine("ReplacingMergeTree", F("version"))`` reads a model field;
    strings are SQL string literals, never column references. ``RawSQLTerm`` arguments
    remain literal SQL. Their dependencies can be declared in ``read_fields``.

    ``replica_path`` and ``replica_name`` are the leading arguments of Replicated/Shared
    engines. Omit both to use server defaults. A shadow database prefixes an explicit
    path without inspecting other arguments. Auxiliary Keeper names are preserved.

    ``sql`` preserves a complete opaque engine clause, primarily for catalog output.
    In that form ``name``, ``read_fields`` and replication attributes describe its
    properties explicitly; SQL is never rewritten. A replicated raw engine cannot be
    used to create shadow storage: declare its path structurally instead.
    """

    name: str
    arguments: tuple[Any, ...]
    replica_path: str | None
    replica_name: str | None
    read_fields: tuple[str, ...]
    sql: RawSQLTerm | None

    def __init__(
        self,
        name: str,
        *arguments: Any,
        replica_path: str | None = None,
        replica_name: str | None = None,
        read_fields: Iterable[str] = (),
        sql: RawSQLTerm | None = None,
    ) -> None:
        if not name.isascii() or not name.isidentifier():
            raise ConfigurationError(ENGINE_INVALID_NAME.format(name=name))
        if (replica_path is None) != (replica_name is None) or (
            replica_path is not None and not name.startswith(CLICKHOUSE_ENGINE_PREFIXES)
        ):
            raise ConfigurationError(ENGINE_REPLICA_ARGUMENTS)
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "arguments", deepcopy(arguments))
        object.__setattr__(self, "replica_path", replica_path)
        object.__setattr__(self, "replica_name", replica_name)
        object.__setattr__(self, "read_fields", tuple(read_fields))
        object.__setattr__(self, "sql", sql)

    def get_family(self) -> str:
        """Return the declared engine family without a replication prefix."""
        for prefix in CLICKHOUSE_ENGINE_PREFIXES:
            if self.name.startswith(prefix):
                return self.name.removeprefix(prefix)
        return self.name

    def is_merge_tree(self) -> bool:
        family = self.get_family()
        return family == "MergeTree" or family in CLICKHOUSE_FINAL_ENGINES

    def is_replicated(self) -> bool:
        return self.name.startswith(CLICKHOUSE_ENGINE_PREFIXES) and self.is_merge_tree()

    def get_sql(self, model: type[Model], dialect: Dialect) -> str:
        """Render arguments once, binding expressions to the table's actual columns."""
        if self.sql is not None:
            return self.sql.sql
        arguments = [self.get_argument_sql(argument, model, dialect) for argument in self.arguments]
        if self.replica_path is not None:
            arguments[:0] = [
                dialect.literals.get_string_literal_sql(self.replica_path),
                dialect.literals.get_literal_sql(self.replica_name),
            ]
        return f"{self.name}({', '.join(arguments)})" if arguments else self.name

    @classmethod
    def get_argument_sql(cls, argument: Any, model: type[Model], dialect: Dialect) -> str:
        """Render a literal, expression or tuple of engine arguments without parsing SQL."""
        if isinstance(argument, (Expression, RawSQLTerm)):
            return DeclaredExpressions.get_sql(argument, model, dialect)
        if isinstance(argument, tuple):
            return f"({', '.join(cls.get_argument_sql(item, model, dialect) for item in argument)})"
        return dialect.literals.get_literal_sql(argument)

    def get_column_names(self, model: type[Model], dialect: Dialect) -> frozenset[str]:
        """Read expression dependencies and explicitly supplied fields, never SQL text."""
        columns: set[str] = set()
        for field_name in self.read_fields:
            columns.update(DeclaredExpressions.get_column_names(field_name, model, dialect) or ())
        pending = list(self.arguments)
        while pending:
            argument = pending.pop()
            if isinstance(argument, Expression):
                columns.update(DeclaredExpressions.get_column_names(argument, model, dialect) or ())
            elif isinstance(argument, tuple):
                pending.extend(argument)
        return frozenset(columns)

    def with_renamed_field(self, old_name: str, new_name: str) -> Self:
        """Rename expression references and explicit dependencies, preserving literal arguments."""
        _, arguments, options = self.deconstruct()
        options["read_fields"] = tuple(new_name if name == old_name else name for name in self.read_fields)
        return type(self)(
            self.name,
            *ExpressionFieldRenaming.get_renamed(arguments[1:], old_name, new_name),
            **options,
        )

    def with_field_names(self, column_to_field_name: Mapping[str, str]) -> Self:
        """Bind catalog column references to generated Python field names in one pass."""
        _, arguments, options = self.deconstruct()
        options["read_fields"] = tuple(column_to_field_name.get(name, name) for name in self.read_fields)
        return type(self)(
            self.name,
            *ExpressionTraversal.map_fields(
                arguments[1:], lambda reference: column_to_field_name.get(reference.path, reference.path)
            ),
            **options,
        )

    def for_shadow(self, prefix: str, replica_defaults: Mapping[str, str] | None = None) -> Self:
        """Isolate explicit paths; accept server defaults only when they identify each shadow.

        Default arguments stay omitted: Replicated databases can prohibit explicit
        Keeper arguments even when the default path safely uses a table UUID.
        """
        if self.sql is not None and self.is_replicated():
            raise UnSupportedError(ENGINE_RAW_SHADOW_PATH.format(name=self.name))
        if self.replica_path is None and not self.is_replicated():
            return self
        _, arguments, options = self.deconstruct()
        if self.replica_path is None:
            if replica_defaults is None or "default_replica_path" not in replica_defaults:
                raise UnSupportedError(ENGINE_MISSING_REPLICA_DEFAULTS)
            path = replica_defaults["default_replica_path"]
            if CLICKHOUSE_UUID_MACRO not in path and "{database}" not in path:
                raise UnSupportedError(ENGINE_UNSCOPED_REPLICA_DEFAULT)
            return self
        path = self.replica_path
        keeper, separator, path = path.partition(":")
        if not separator:
            keeper, path = "", keeper
        options["replica_path"] = f"{keeper}{separator}/{prefix}/{path.lstrip('/')}"
        return type(self)(*arguments, **options)

    def deconstruct(self) -> tuple[str, list[Any], dict[str, Any]]:
        options: dict[str, Any] = {}
        if self.replica_path is not None:
            options.update(replica_path=self.replica_path, replica_name=self.replica_name)
        if self.read_fields:
            options["read_fields"] = self.read_fields
        if self.sql is not None:
            options["sql"] = self.sql
        return ClassPath.get(type(self)), [self.name, *deepcopy(self.arguments)], options
