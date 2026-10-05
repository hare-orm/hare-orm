from __future__ import annotations

import dataclasses
from collections.abc import Callable, Mapping, Sequence
from typing import TYPE_CHECKING, Any, ClassVar, Self

from hare.ddl.raw_sql_term import RawSQLTerm
from hare.ddl.table_options import TableOptions
from hare.dialects.clickhouse.cluster.constants import (
    CLICKHOUSE_DISTRIBUTED_ENGINE_TEMPLATE,
    CLICKHOUSE_DISTRIBUTED_TABLE_TEMPLATE,
)
from hare.dialects.clickhouse.enums import ClickhouseDialectName
from hare.dialects.clickhouse.query.constants import CLICKHOUSE_ENGINE_PREFIXES, CLICKHOUSE_FINAL_ENGINES
from hare.dialects.clickhouse.schema.constants import (
    CLICKHOUSE_CODEC_PATTERN,
    CLICKHOUSE_LIGHTWEIGHT_UPDATE_SETTING_VALUE,
    CLICKHOUSE_LIGHTWEIGHT_UPDATE_SETTINGS,
    CLICKHOUSE_MERGE_TREE_ENGINE_SUFFIX,
    CLICKHOUSE_MERGE_TREE_OPTIONS,
    CLICKHOUSE_PROJECTION_REBUILD_MODE,
    CLICKHOUSE_PROJECTION_REBUILD_SETTINGS,
)
from hare.dialects.clickhouse.schema_objects.clickhouse_projection import ClickhouseProjection
from hare.exceptions import ConfigurationError, QueryError, UnSupportedError

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.dialects.base.dialect import Dialect
    from hare.dialects.base.features import Features
    from hare.models import Model


@dataclasses.dataclass(frozen=True)
class ClickhouseTableOptions(TableOptions):
    """How ClickHouse stores a model's table - its engine and how the engine keeps the rows::

        class PageView(Model):
            class Meta:
                table_options = [
                    ClickhouseTableOptions(
                        order_by=("site", "viewed_at"), partition_by=RawSQLTerm("toYYYYMM(viewed_at)")
                    )
                ]

    A model without these options is stored by a ``MergeTree`` sorted by its primary key.

    Attributes:
        engine: The table engine, with its arguments - ``"MergeTree"``,
            ``"ReplacingMergeTree(version)"``.
        order_by: What the rows are sorted by - field names, and ``RawSQLTerm`` SQL expressions
            (``RawSQLTerm("intHash32(id)")``, the key of a sample); empty for the primary key, or for
            no sort (``tuple()``) without one. A primary key must be the beginning of it.
        sample_by: What ``sample()`` picks rows by - one of ``order_by``, a field name or the
            ``RawSQLTerm`` expression; None for a table read in no sample.
        partition_by: ``RawSQLTerm`` of the SQL expression the rows are partitioned by, None for
            one partition.
        ttl: ``RawSQLTerm`` of the table's ``TTL`` clause, None for none.
        settings: The table's ``SETTINGS``, by name.
        column_codecs: The compression of columns - ``(field name, codecs)`` pairs, the codecs as
            ``CODEC(...)`` takes them (``"ZSTD(3)"``, ``"Delta, ZSTD"``).
        column_ttls: How long columns keep their values - ``(field name, RawSQLTerm)`` pairs, the
            expression of the moment a value is reset to its default; not of a key column.
        projections: The table's projections (``ClickhouseProjection``) - its rows kept again inside
            each part, sorted or aggregated for the queries that fit.
        distributed_over: The name of the local table storing the rows on every server of the
            connection's cluster - the model's own table is then a ``Distributed`` one over it: read
            and written through, its rows changed and its storage kept in the local tables.
        sharding_key: ``RawSQLTerm`` of the expression a row's shard is picked by; None for any shard.
        lightweight_updates: Change the rows by a lightweight ``UPDATE`` - their new values written
            beside them and read in place of the old ones at once - instead of a mutation rewriting each
            part holding one (ClickHouse 25.7).
    """

    dialect_name: ClassVar[str] = ClickhouseDialectName.CLICKHOUSE

    engine: str = "MergeTree"
    order_by: tuple[str | RawSQLTerm, ...] = ()
    sample_by: str | RawSQLTerm | None = None
    partition_by: RawSQLTerm | None = None
    ttl: RawSQLTerm | None = None
    settings: tuple[tuple[str, str | int], ...] = ()
    column_codecs: tuple[tuple[str, str], ...] = ()
    column_ttls: tuple[tuple[str, RawSQLTerm], ...] = ()
    projections: tuple[ClickhouseProjection, ...] = ()
    distributed_over: str | None = None
    sharding_key: RawSQLTerm | None = None
    lightweight_updates: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.engine, str) or not self.engine.strip():
            raise ConfigurationError(f"ClickhouseTableOptions.engine must be a non-empty string, got {self.engine!r}")
        if not isinstance(self.order_by, tuple) or not all(
            (isinstance(key, str) and key) or (isinstance(key, RawSQLTerm) and key.sql.strip())
            for key in self.order_by
        ):
            raise ConfigurationError(
                "ClickhouseTableOptions.order_by must be a tuple of field names and RawSQLTerm(...) expressions, "
                f"got {self.order_by!r}"
            )
        if self.sample_by is not None and self.sample_by not in self.order_by:
            raise ConfigurationError(
                f"ClickhouseTableOptions.sample_by {self.sample_by!r} must be one of order_by - ClickHouse "
                "samples rows by a key they are sorted by"
            )
        if self.distributed_over is not None and (
            not isinstance(self.distributed_over, str) or not self.distributed_over.strip()
        ):
            raise ConfigurationError(
                f"ClickhouseTableOptions.distributed_over must be a table name, got {self.distributed_over!r}"
            )
        if self.sharding_key is not None and self.distributed_over is None:
            raise ConfigurationError(
                "ClickhouseTableOptions.sharding_key is of a table distributed over a local one - distributed_over"
            )
        if not isinstance(self.lightweight_updates, bool):
            raise ConfigurationError(
                f"ClickhouseTableOptions.lightweight_updates must be a bool, got {self.lightweight_updates!r}"
            )
        for option_name in ("partition_by", "ttl", "sharding_key"):
            value = getattr(self, option_name)
            if value is not None and (not isinstance(value, RawSQLTerm) or not value.sql.strip()):
                raise ConfigurationError(
                    f"ClickhouseTableOptions.{option_name} takes RawSQLTerm(...) of a non-empty SQL expression, "
                    f"got {value!r}"
                )
        if not isinstance(self.settings, tuple) or not all(
            isinstance(setting, tuple)
            and len(setting) == 2
            and isinstance(setting[0], str)
            and setting[0].isidentifier()
            and isinstance(setting[1], str | int)
            and not isinstance(setting[1], bool)
            for setting in self.settings
        ):
            raise ConfigurationError(
                "ClickhouseTableOptions.settings must be a tuple of (name, value) pairs, a value a string or "
                f"an integer, got {self.settings!r}"
            )
        if not isinstance(self.column_codecs, tuple) or not all(
            isinstance(entry, tuple)
            and len(entry) == 2
            and isinstance(entry[0], str)
            and isinstance(entry[1], str)
            and CLICKHOUSE_CODEC_PATTERN.fullmatch(entry[1])
            for entry in self.column_codecs
        ):
            raise ConfigurationError(
                "ClickhouseTableOptions.column_codecs must be a tuple of (field name, codecs) pairs, the codecs "
                f'as CODEC(...) takes them - "ZSTD(3)", "Delta, ZSTD" - got {self.column_codecs!r}'
            )
        if not isinstance(self.column_ttls, tuple) or not all(
            isinstance(entry, tuple)
            and len(entry) == 2
            and isinstance(entry[0], str)
            and isinstance(entry[1], RawSQLTerm)
            and entry[1].sql.strip()
            for entry in self.column_ttls
        ):
            raise ConfigurationError(
                "ClickhouseTableOptions.column_ttls must be a tuple of (field name, RawSQLTerm(...)) pairs, got "
                f"{self.column_ttls!r}"
            )
        projection_names = [getattr(projection, "name", None) for projection in self.projections]
        if (
            not isinstance(self.projections, tuple)
            or not all(isinstance(projection, ClickhouseProjection) for projection in self.projections)
            or len(set(projection_names)) != len(projection_names)
        ):
            raise ConfigurationError(
                "ClickhouseTableOptions.projections must be a tuple of ClickhouseProjection(...) of distinct "
                f"names, got {self.projections!r}"
            )

    def raise_if_unsupported(self, model: type[Model], features: Features) -> None:
        """Rejects a sort that doesn't begin with the primary key, or names no field of the model.

        Args:
            model: The model.
            features: The features of the connection.

        Raises:
            ConfigurationError: ``order_by`` names an unknown field or one computed on read, or doesn't
                begin with the key; ``column_codecs`` or ``column_ttls`` name an unknown field, or
                ``column_ttls`` a key.
        """
        meta = model._meta
        unknown = [name for name in self.order_by if isinstance(name, str) and name not in meta.fields_db_projection]
        if unknown:
            raise ConfigurationError(f"{model.__name__}: ClickhouseTableOptions.order_by names no field {unknown}")
        computed_on_read = [
            name
            for name in self.order_by
            if isinstance(name, str) and getattr(meta.fields_map[name], "stored", True) is False
        ]
        if computed_on_read:
            raise ConfigurationError(
                f"{model.__name__}: ClickhouseTableOptions.order_by names {computed_on_read} - a column computed on "
                "read (GeneratedField(stored=False), an ALIAS) is no key the rows are sorted by"
            )
        column_option_names = [name for name, _ in (*self.column_codecs, *self.column_ttls)]
        unknown_columns = [name for name in column_option_names if name not in meta.fields_db_projection]
        if unknown_columns:
            raise ConfigurationError(
                f"{model.__name__}: ClickhouseTableOptions.column_codecs/column_ttls name no field {unknown_columns}"
            )
        key_names = {*meta.primary_key_attribute_names, *(name for name in self.order_by if isinstance(name, str))}
        expiring_keys = [name for name, _ in self.column_ttls if name in key_names]
        if expiring_keys:
            raise ConfigurationError(
                f"{model.__name__}: ClickhouseTableOptions.column_ttls names {expiring_keys} - a column of the "
                "table's key keeps its values"
            )
        if not self.is_merge_tree():
            merge_tree_options = [
                option_name
                for option_name in CLICKHOUSE_MERGE_TREE_OPTIONS
                if getattr(self, option_name) != getattr(ClickhouseTableOptions, option_name)
            ]
            if merge_tree_options:
                raise ConfigurationError(
                    f"{model.__name__}: ClickhouseTableOptions.{', '.join(merge_tree_options)} are options of "
                    f"an engine of the MergeTree family, not of {self.get_engine_name()}"
                )
        if self.lightweight_updates and not features.supports_lightweight_update:
            raise UnSupportedError(
                f"{model.__name__}: ClickhouseTableOptions(lightweight_updates=True) needs ClickHouse 25.7 or later"
            )
        if self.order_by and meta.has_primary_key:
            pk_names = list(meta.primary_key_attribute_names)
            if list(self.order_by[: len(pk_names)]) != pk_names:
                raise ConfigurationError(
                    f"{model.__name__}: ClickhouseTableOptions.order_by must begin with the primary key {pk_names}"
                )

    def with_field_names(self, column_to_field_name: Mapping[str, str]) -> Self:
        return dataclasses.replace(
            self,
            order_by=tuple(
                column_to_field_name.get(key, key) if isinstance(key, str) else key for key in self.order_by
            ),
            sample_by=column_to_field_name.get(self.sample_by, self.sample_by)
            if isinstance(self.sample_by, str)
            else self.sample_by,
            column_codecs=tuple((column_to_field_name.get(name, name), codecs) for name, codecs in self.column_codecs),
            column_ttls=tuple((column_to_field_name.get(name, name), ttl) for name, ttl in self.column_ttls),
        )

    @classmethod
    def get_for_model(cls, model: type[Model] | None, dialect: Dialect) -> Self:
        """The ClickHouse options of a model's table.

        Args:
            model: The model, None for a table of no model.
            dialect: The dialect.

        Returns:
            The declared options - the defaults for a table of no model, and for a model declaring
            none.
        """
        return cls.get_of_dialect(() if model is None else model._meta.table_options, dialect)

    @classmethod
    def get_of_dialect(cls, table_options: Sequence[TableOptions], dialect: Dialect) -> Self:
        """The ClickHouse entry of a model's ``Meta.table_options``.

        Args:
            table_options: The entries.
            dialect: The dialect.

        Returns:
            The entry - the defaults where there is none.
        """
        options = next(
            (options for options in table_options if options.dialect_name == dialect.name),
            dialect.default_table_options,
        )
        return options if isinstance(options, cls) else cls()

    def get_storage_table_name(self, table_name: str) -> str:
        return table_name if self.distributed_over is None else self.distributed_over

    def get_companion_table_sqls(
        self, model: type[Model], quote: Callable[[str], str], client: DatabaseClient, *, replaces: bool
    ) -> list[str]:
        """The ``Distributed`` table of a model stored in local tables over a cluster.

        Raises:
            ConfigurationError: The connection names no cluster.
        """
        if self.distributed_over is None:
            return []
        cluster = getattr(client, "cluster", None)
        if cluster is None:
            raise ConfigurationError(
                f"{model.__name__}: ClickhouseTableOptions.distributed_over needs a connection to a cluster - "
                "its cluster=... setting"
            )
        meta = model._meta
        literals = client.dialect.literals
        engine_sql = CLICKHOUSE_DISTRIBUTED_ENGINE_TEMPLATE.format(
            cluster=cluster.name,
            table=literals.get_string_literal_sql(self.distributed_over),
            sharding_key="" if self.sharding_key is None else f", {self.sharding_key.sql}",
        )
        return [
            CLICKHOUSE_DISTRIBUTED_TABLE_TEMPLATE.format(
                or_replace="OR REPLACE " if replaces else "",
                exists="" if replaces else "IF NOT EXISTS ",
                table=literals.qualify_table_name(meta.db_table, meta.schema),
                local_table=literals.qualify_table_name(self.distributed_over, meta.schema),
                engine=engine_sql,
            )
        ]

    def get_key_sql(self, model: type[Model], key: str | RawSQLTerm, quote: Callable[[str], str]) -> str:
        """A key of the table's sort in the SQL text.

        Args:
            model: The model.
            key: A field name or an expression.
            quote: Quotes an identifier.

        Returns:
            The field's column, quoted, or the expression.
        """
        return key.sql if isinstance(key, RawSQLTerm) else quote(model._meta.fields_db_projection[key])

    def get_column_clauses_sql(self, field_name: str) -> str:
        clauses_sql = ""
        for codec_field_name, codecs in self.column_codecs:
            if codec_field_name == field_name:
                clauses_sql += f" CODEC({codecs})"
        for ttl_field_name, ttl in self.column_ttls:
            if ttl_field_name == field_name:
                clauses_sql += f" TTL {ttl.sql}"
        return clauses_sql

    def get_projection_settings_sql(self, features: Features) -> str:
        """The settings a table with projections takes a lightweight ``DELETE`` and merges dropping
        rows by - its projections rebuilt for the parts the rows leave.

        Args:
            features: The features of the connection.

        Returns:
            ``name = value, ...`` of the settings the options don't declare themselves; empty for a
            table without projections and a server without the settings.
        """
        if not self.projections or not features.rebuilds_projections:
            return ""
        declared_settings = dict(self.settings)
        mode_sql = self.get_setting_sql(CLICKHOUSE_PROJECTION_REBUILD_MODE)
        return ", ".join(
            f"{name} = {mode_sql}" for name in CLICKHOUSE_PROJECTION_REBUILD_SETTINGS if name not in declared_settings
        )

    def get_after_create_sqls(
        self, model: type[Model], table_sql: str, quote: Callable[[str], str], features: Features
    ) -> list[str]:
        statements = []
        # First: an engine dropping rows on merges takes a projection only with them.
        if settings_sql := self.get_projection_settings_sql(features):
            statements.append(f"ALTER TABLE {table_sql} MODIFY SETTING {settings_sql};")
        statements.extend(
            f"ALTER TABLE {table_sql} ADD PROJECTION {projection.get_definition_sql(quote)};"
            for projection in self.projections
        )
        return statements

    def get_create_suffix_sql(self, model: type[Model], quote: Callable[[str], str]) -> str:
        meta = model._meta
        sql = f" ENGINE = {self.engine}"
        if not self.is_merge_tree():
            # An engine of no sort, partitions or time to live - Memory, Log, Join, ...
            return sql
        if self.partition_by is not None:
            sql += f" PARTITION BY {self.partition_by.sql}"
        if self.order_by:
            sql += f" ORDER BY ({', '.join(self.get_key_sql(model, key, quote) for key in self.order_by)})"
        elif not meta.has_primary_key:
            sql += " ORDER BY tuple()"
        if primary_key_sqls := self.get_primary_key_sqls(model, quote):
            sql += f" PRIMARY KEY ({', '.join(primary_key_sqls)})"
        if self.sample_by is not None:
            sql += f" SAMPLE BY {self.get_key_sql(model, self.sample_by, quote)}"
        if self.ttl is not None:
            sql += f" TTL {self.ttl.sql}"
        if settings := self.get_table_settings():
            sql += " SETTINGS " + ", ".join(f"{name} = {self.get_setting_sql(value)}" for name, value in settings)
        return sql

    def get_table_settings(self) -> list[tuple[str, str | int]]:
        """The settings the table is created with.

        Returns:
            The declared settings, with those of a table changed by lightweight updates.
        """
        settings = list(self.settings)
        if self.lightweight_updates:
            declared_names = dict(self.settings)
            settings.extend(
                (name, CLICKHOUSE_LIGHTWEIGHT_UPDATE_SETTING_VALUE)
                for name in CLICKHOUSE_LIGHTWEIGHT_UPDATE_SETTINGS
                if name not in declared_names
            )
        return settings

    def get_primary_key_sqls(self, model: type[Model], quote: Callable[[str], str]) -> list[str]:
        """The keys of the table's ``PRIMARY KEY`` - the model's primary key columns, and the keys of
        ``order_by`` after them up to ``sample_by``: ClickHouse samples by a key of the primary key.

        Args:
            model: The model.
            quote: Quotes an identifier.

        Returns:
            The keys' SQL, empty for a model without a primary key.
        """
        meta = model._meta
        if not meta.has_primary_key:
            return []
        keys: list[str | RawSQLTerm] = list(meta.primary_key_attribute_names)
        if self.sample_by is not None and self.sample_by not in keys:
            keys = list(self.order_by[: self.order_by.index(self.sample_by) + 1])
        return [self.get_key_sql(model, key, quote) for key in keys]

    def raise_if_unsampled(self, model: type[Model]) -> None:
        """Rejects a sample of a table declaring no key to sample its rows by.

        Args:
            model: The model.

        Raises:
            QueryError: ``sample_by`` isn't set.
        """
        if self.sample_by is None:
            raise QueryError(
                f"{model.__name__}.objects.sample() needs ClickhouseTableOptions(sample_by=RawSQLTerm(...)) - "
                "ClickHouse samples the rows of a table by a key of its sort"
            )

    def is_merge_tree(self) -> bool:
        """Whether the engine is one of the MergeTree family - a table sorted, partitioned and kept in
        parts.

        Returns:
            Whether it is.
        """
        return self.get_engine_name().endswith(CLICKHOUSE_MERGE_TREE_ENGINE_SUFFIX)

    def keeps_row_versions(self) -> bool:
        """Whether the engine keeps several versions of a row its merges and ``FINAL`` merge into one -
        ``ReplacingMergeTree``, ``CollapsingMergeTree`` and the other engines of their families.

        Returns:
            Whether it does.
        """
        family_name = self.get_engine_name()
        for prefix in CLICKHOUSE_ENGINE_PREFIXES:
            family_name = family_name.removeprefix(prefix)
        return family_name in CLICKHOUSE_FINAL_ENGINES

    def get_engine_name(self) -> str:
        """The engine's name, without its arguments.

        Returns:
            The name - ``"ReplacingMergeTree"`` of ``"ReplacingMergeTree(version)"``.
        """
        return self.engine.partition("(")[0].strip()

    @staticmethod
    def get_setting_sql(value: Any) -> str:
        """A setting's value in the SQL text.

        Args:
            value: A string, a bool, an int or a float.

        Returns:
            The literal.
        """
        # Local import: the constants build the dialect, which declares these options.
        from hare.dialects.clickhouse.constants import CLICKHOUSE_STRING_ESCAPES

        if isinstance(value, bool):
            return "1" if value else "0"
        if isinstance(value, int | float):
            return repr(value)
        return "'" + value.translate(CLICKHOUSE_STRING_ESCAPES) + "'"
