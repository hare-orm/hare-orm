from __future__ import annotations

import dataclasses
from collections.abc import Iterable, Mapping, Sequence
from typing import TYPE_CHECKING, Any, ClassVar, Self, cast

from hare.classes.comparable_values import ComparableValues
from hare.classes.value_descriptions import ValueDescriptions
from hare.core.caching.cache import Cache
from hare.ddl.constants import FIXED_FIELDS_WRITTEN_MESSAGE
from hare.ddl.expressions.declared_expressions import DeclaredExpressions
from hare.ddl.expressions.expression_field_renaming import ExpressionFieldRenaming
from hare.ddl.raw_sql_term import RawSQLTerm
from hare.ddl.table_options import TableOptions
from hare.dialects.clickhouse.cluster.constants import (
    CLICKHOUSE_DISTRIBUTED_ENGINE_TEMPLATE,
    CLICKHOUSE_DISTRIBUTED_TABLE_TEMPLATE,
)
from hare.dialects.clickhouse.introspection.clickhouse_sql_parts import ClickhouseSqlParts
from hare.dialects.clickhouse.introspection.constants import CLICKHOUSE_CURRENT_DATABASE_SQL_FUNCTION
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
from hare.dialects.clickhouse.types.clickhouse_type_names import ClickhouseTypeNames
from hare.dialects.clickhouse.types.constants import CLICKHOUSE_UNSIGNED_TYPE_PREFIX
from hare.exceptions import ConfigurationError, QueryError, UnSupportedError
from hare.fields.relations.fields.foreign_key_field_instance import ForeignKeyFieldInstance

if TYPE_CHECKING:
    from hare.dialects.base.dialect import Dialect
    from hare.dialects.base.features import Features
    from hare.dialects.base.schema.base_schema_editor import BaseSchemaEditor
    from hare.models import Model
    from hare.models.meta_info import MetaInfo
    from hare.query.expressions import Expression
    from hare.query.queryset.queryset import QuerySet


@dataclasses.dataclass(frozen=True)
class ClickhouseTableOptions(TableOptions):
    """How ClickHouse stores a model's table - its engine and how the engine keeps the rows::

        class PageView(Model):
            class Meta:
                table_options = [
                    ClickhouseTableOptions(order_by=("site", "viewed_at"), partition_by=Trunc("viewed_at", "month"))
                ]

    A model without these options is stored by a ``MergeTree`` sorted by its primary key. Every key and
    time to live is a field's name, an expression of hare over the model's fields (``F``, a function,
    arithmetic) - written in ClickHouse's SQL, renamed with the fields it reads, its columns known to hare -
    or ``RawSQLTerm`` of raw SQL, written as it is, its columns not read out of it.

    Attributes:
        engine: The table engine, with its arguments - ``"MergeTree"``,
            ``"ReplacingMergeTree(version)"``.
        order_by: What the rows are sorted by - field names and expressions (``Trunc("viewed_at",
            "day")``, ``RawSQLTerm("intHash32(id)")`` the key of a sample); empty for the primary key, or for
            no sort (``tuple()``) without one. A primary key must be the beginning of it.
        sample_by: What ``sample()`` picks rows by - one of ``order_by``; None for a table read in no
            sample.
        partition_by: The expression the rows are partitioned by (``Trunc("viewed_at", "month")``), None
            for one partition.
        ttl: The table's ``TTL`` - an expression of the moment a row is deleted
            (``F("viewed_at") + timedelta(days=30)``), or ``RawSQLTerm`` of a whole clause with another
            action; None for none.
        settings: The table's ``SETTINGS``, by name.
        column_codecs: The compression of columns - ``(field name, codecs)`` pairs, the codecs as
            ``CODEC(...)`` takes them (``"ZSTD(3)"``, ``"Delta, ZSTD"``).
        column_ttls: How long columns keep their values - ``(field name, expression)`` pairs, the
            expression of the moment a value is reset to its default; not of a key column.
        projections: The table's projections (``ClickhouseProjection``) - its rows kept again inside
            each part, sorted or aggregated for the queries that fit.
        distributed_over: The name of the local table storing the rows on every server of the
            connection's cluster - the model's own table is then a ``Distributed`` one over it: read
            and written through, its rows changed and its storage kept in the local tables.
        sharding_key: The expression a row's shard is picked by; None for any shard.
        lightweight_updates: Change the rows by a lightweight ``UPDATE`` - their new values written
            beside them and read in place of the old ones at once - instead of a mutation rewriting each
            part holding one (ClickHouse 25.7).
    """

    #: The fields of the columns ClickHouse doesn't update in place, by the options - ``get_fixed_field_names()``.
    fixed_field_names: ClassVar[Cache[Any]] = Cache(holds_sql=False, keyed_by_model=False)

    engine: str = "MergeTree"
    order_by: tuple[str | Expression | RawSQLTerm, ...] = ()
    sample_by: str | Expression | RawSQLTerm | None = None
    partition_by: str | Expression | RawSQLTerm | None = None
    ttl: str | Expression | RawSQLTerm | None = None
    settings: tuple[tuple[str, str | int], ...] = ()
    column_codecs: tuple[tuple[str, str], ...] = ()
    column_ttls: tuple[tuple[str, str | Expression | RawSQLTerm], ...] = ()
    projections: tuple[ClickhouseProjection, ...] = ()
    distributed_over: str | None = None
    sharding_key: str | Expression | RawSQLTerm | None = None
    lightweight_updates: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.engine, str) or not self.engine.strip():
            raise ConfigurationError(f"ClickhouseTableOptions.engine must be a non-empty string, got {self.engine!r}")
        if not isinstance(self.order_by, tuple) or not all(
            DeclaredExpressions.is_declared_expression(key) for key in self.order_by
        ):
            raise ConfigurationError(
                "ClickhouseTableOptions.order_by must be a tuple of field names, expressions of hare and "
                f"RawSQLTerm(...) expressions, got {ValueDescriptions.get_shown_value(self.order_by)}"
            )
        if self.sample_by is not None and self.get_order_by_position(self.sample_by) is None:
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
        for option_name in ("partition_by", "ttl", "sharding_key"):
            value = getattr(self, option_name)
            if value is not None and not DeclaredExpressions.is_declared_expression(value):
                raise ConfigurationError(
                    f"ClickhouseTableOptions.{option_name} takes a field name, an expression of hare or "
                    f"RawSQLTerm(...) of a non-empty SQL expression, got {ValueDescriptions.get_shown_value(value)}"
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
                'as CODEC(...) takes them - "ZSTD(3)", "Delta, ZSTD" - got '
                f"{ValueDescriptions.get_shown_value(self.column_codecs)}"
            )
        if not isinstance(self.column_ttls, tuple) or not all(
            isinstance(entry, tuple)
            and len(entry) == 2
            and isinstance(entry[0], str)
            and DeclaredExpressions.is_declared_expression(entry[1])
            for entry in self.column_ttls
        ):
            raise ConfigurationError(
                "ClickhouseTableOptions.column_ttls must be a tuple of (field name, expression) pairs, got "
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
                f"names, got {ValueDescriptions.get_shown_value(self.projections)}"
            )

    def raise_if_unsupported(self, model: type[Model], features: Features) -> None:
        """Rejects a sort that doesn't begin with the primary key, or names no field of the model.

        Args:
            model: The model.
            features: The features of the connection.

        Raises:
            ConfigurationError: ``order_by``, ``sample_by``, ``partition_by``, ``ttl``, ``sharding_key`` or a
                ``column_ttls`` time names an unknown field, ``order_by`` one computed on read or doesn't
                begin with the key; ``sample_by`` names a field of no unsigned integer; ``column_codecs``
                or ``column_ttls`` name an unknown field, or ``column_ttls`` a key.
        """
        meta = model._meta
        # A text names a field in each of these - one of no field met the DDL as a KeyError.
        for option_name, keys in (
            ("order_by", self.order_by),
            ("sample_by", (self.sample_by,)),
            ("partition_by", (self.partition_by,)),
            ("ttl", (self.ttl,)),
            ("sharding_key", (self.sharding_key,)),
            ("column_ttls", tuple(ttl for _, ttl in self.column_ttls)),
        ):
            unknown = [key for key in keys if isinstance(key, str) and key not in meta.fields_db_projection]
            if unknown:
                raise ConfigurationError(
                    f"{model.__name__}: ClickhouseTableOptions.{option_name} names no field {unknown}"
                )
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
        if isinstance(self.sample_by, str):
            # Local import: the dialect module imports this one.
            from hare.dialects.clickhouse.constants import CLICKHOUSE_DIALECT

            sample_field = meta.fields_map[self.sample_by]
            sample_column_type = ClickhouseTypeNames.get_server_type(sample_field.get_column_type(CLICKHOUSE_DIALECT))
            if sample_field.null or not sample_column_type.startswith(CLICKHOUSE_UNSIGNED_TYPE_PREFIX):
                raise ConfigurationError(
                    f"{model.__name__}: ClickhouseTableOptions.sample_by {self.sample_by!r} is a column of "
                    f"{'Nullable ' if sample_field.null else ''}{sample_column_type} - ClickHouse samples rows by an "
                    "unsigned integer that holds no NULL (a UInt field, or RawSQLTerm('intHash32(...)'))"
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

    def with_renamed_field(self, old_name: str, new_name: str) -> Self:
        # A field name and the fields an expression of hare reads are renamed; raw SQL is left as it is.
        renamed = super().with_renamed_field(old_name, new_name)

        def get_renamed(value: Any) -> Any:
            return ExpressionFieldRenaming.get_renamed(value, old_name, new_name)

        return dataclasses.replace(
            renamed,
            order_by=tuple(get_renamed(key) for key in renamed.order_by),
            sample_by=get_renamed(renamed.sample_by),
            partition_by=get_renamed(renamed.partition_by),
            ttl=get_renamed(renamed.ttl),
            sharding_key=get_renamed(renamed.sharding_key),
            column_ttls=tuple((name, get_renamed(ttl)) for name, ttl in renamed.column_ttls),
        )

    def get_order_by_position(self, key: Any) -> int | None:
        """Where a key is in ``order_by`` - an expression found by its declaration, not by identity.

        Args:
            key: A field name or an expression.

        Returns:
            Its position, None when ``order_by`` doesn't hold it.
        """
        for position, order_key in enumerate(self.order_by):
            if ComparableValues.are_equal(order_key, key):
                return position
        return None

    def get_distributed_engine_sql(
        self, model: type[Model], editor: BaseSchemaEditor, cluster_name: str, local_table: str
    ) -> str:
        """The ``Distributed`` engine spreading the rows of a local table of the model's database over the
        connection's cluster.

        Args:
            model: The model.
            editor: The schema editor.
            cluster_name: The cluster's name.
            local_table: The local table.

        Returns:
            The engine - naming ``currentDatabase()`` for a model of the connection's database.
        """
        literals = editor.client.dialect.literals
        schema = model._meta.schema
        return CLICKHOUSE_DISTRIBUTED_ENGINE_TEMPLATE.format(
            cluster=cluster_name,
            database=CLICKHOUSE_CURRENT_DATABASE_SQL_FUNCTION
            if schema is None
            else literals.get_string_literal_sql(schema),
            table=literals.get_string_literal_sql(local_table),
            sharding_key=self.get_sharding_key_sql(model, editor),
        )

    def get_sharding_key_sql(self, model: type[Model], editor: BaseSchemaEditor) -> str:
        """The argument a ``Distributed`` engine takes the sharding key by.

        Args:
            model: The model.
            editor: The schema editor of the connection the DDL runs on.

        Returns:
            ``, <key>``; empty for a table of no sharding key.
        """
        if self.sharding_key is None:
            return ""
        return f", {self.get_expression_sql(model, self.sharding_key, editor)}"

    def with_sql_queries(self, model: type[Model]) -> Self:
        if not self.projections:
            return self
        return dataclasses.replace(
            self, projections=tuple(projection.with_sql_query(model) for projection in self.projections)
        )

    def with_relation_keys(self, meta: MetaInfo) -> Self:
        # A ModelQuery() of a projection is a queryset of the model just built.
        options = self
        if self.projections:
            options = dataclasses.replace(
                options, projections=tuple(projection.for_model(meta._model) for projection in self.projections)
            )
        # A foreign key or a one-to-one relation sorts by its key fields.
        relations = [meta.fields_map.get(key) if isinstance(key, str) else None for key in self.order_by]
        if not any(isinstance(relation, ForeignKeyFieldInstance) for relation in relations):
            return options
        order_by: list[str | Expression | RawSQLTerm] = []
        for key, relation in zip(self.order_by, relations, strict=True):
            if isinstance(relation, ForeignKeyFieldInstance):
                order_by.extend(relation.source_fields)
            else:
                order_by.append(key)
        return dataclasses.replace(options, order_by=tuple(order_by))

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
        options = dialect.get_table_options(table_options)
        return options if isinstance(options, cls) else cls()

    def get_storage_table_name(self, table_name: str) -> str:
        return table_name if self.distributed_over is None else self.distributed_over

    def get_companion_table_sqls(self, model: type[Model], editor: BaseSchemaEditor, *, replaces: bool) -> list[str]:
        """The ``Distributed`` table of a model stored in local tables over a cluster.

        Raises:
            ConfigurationError: The connection names no cluster.
        """
        if self.distributed_over is None:
            return []
        client = editor.client
        cluster = getattr(client, "cluster", None)
        if cluster is None:
            raise ConfigurationError(
                f"{model.__name__}: ClickhouseTableOptions.distributed_over needs a connection to a cluster - "
                "its cluster=... setting"
            )
        meta = model._meta
        literals = client.dialect.literals
        engine_sql = self.get_distributed_engine_sql(model, editor, cluster.name, self.distributed_over)
        return [
            CLICKHOUSE_DISTRIBUTED_TABLE_TEMPLATE.format(
                or_replace="OR REPLACE " if replaces else "",
                exists="" if replaces else "IF NOT EXISTS ",
                table=literals.qualify_table_name(meta.db_table, meta.schema),
                local_table=literals.qualify_table_name(self.distributed_over, meta.schema),
                engine=engine_sql,
            )
        ]

    @staticmethod
    def get_expression_sql(model: type[Model], expression: Any, editor: BaseSchemaEditor) -> str:
        """A key or a time to live of the options in the SQL text.

        Args:
            model: The model.
            expression: A field name, an expression of hare or a ``RawSQLTerm``.
            editor: The schema editor of the connection the DDL runs on.

        Returns:
            The field's column, quoted; an expression of hare in the connection's SQL; raw SQL as written.
        """
        if isinstance(expression, str):
            return editor.quote(model._meta.fields_db_projection[expression])
        return DeclaredExpressions.get_sql(expression, model, editor.client.dialect)

    def get_column_clauses_sql(self, model: type[Model], field_name: str, editor: BaseSchemaEditor) -> str:
        clauses_sql = ""
        for codec_field_name, codecs in self.column_codecs:
            if codec_field_name == field_name:
                clauses_sql += f" CODEC({codecs})"
        for ttl_field_name, ttl in self.column_ttls:
            if ttl_field_name == field_name:
                clauses_sql += f" TTL {self.get_expression_sql(model, ttl, editor)}"
        return clauses_sql

    def get_projection_settings_sql(self) -> str:
        """The settings a table with projections takes a lightweight ``DELETE`` and merges dropping
        rows by - its projections rebuilt for the parts the rows leave.

        Returns:
            ``name = value, ...`` of the settings the options don't declare themselves; empty for a
            table without projections.
        """
        if not self.projections:
            return ""
        declared_settings = dict(self.settings)
        mode_sql = self.get_setting_sql(CLICKHOUSE_PROJECTION_REBUILD_MODE)
        return ", ".join(
            f"{name} = {mode_sql}" for name in CLICKHOUSE_PROJECTION_REBUILD_SETTINGS if name not in declared_settings
        )

    def get_after_create_sqls(self, model: type[Model], table_sql: str, editor: BaseSchemaEditor) -> list[str]:
        statements = []
        # First: an engine dropping rows on merges takes a projection only with them.
        if settings_sql := self.get_projection_settings_sql():
            statements.append(f"ALTER TABLE {table_sql} MODIFY SETTING {settings_sql};")
        statements.extend(
            f"ALTER TABLE {table_sql} ADD PROJECTION {projection.get_definition_sql(model, editor)};"
            for projection in self.projections
        )
        return statements

    def get_create_suffix_sql(self, model: type[Model], editor: BaseSchemaEditor) -> str:
        meta = model._meta
        sql = f" ENGINE = {self.engine}"
        if not self.is_merge_tree():
            # An engine of no sort, partitions or time to live - Memory, Log, Join, ...
            return sql
        if self.partition_by is not None:
            sql += f" PARTITION BY {self.get_expression_sql(model, self.partition_by, editor)}"
        if self.order_by:
            sql += f" ORDER BY ({', '.join(self.get_expression_sql(model, key, editor) for key in self.order_by)})"
        elif not meta.has_primary_key:
            sql += " ORDER BY tuple()"
        if primary_key_sqls := self.get_primary_key_sqls(model, editor):
            sql += f" PRIMARY KEY ({', '.join(primary_key_sqls)})"
        if self.sample_by is not None:
            sql += f" SAMPLE BY {self.get_expression_sql(model, self.sample_by, editor)}"
        if self.ttl is not None:
            sql += f" TTL {self.get_expression_sql(model, self.ttl, editor)}"
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

    def get_primary_key_sqls(self, model: type[Model], editor: BaseSchemaEditor) -> list[str]:
        """The SQL of the keys of the table's ``PRIMARY KEY`` - see ``get_primary_key_keys()``.

        Args:
            model: The model.
            editor: The schema editor of the connection the DDL runs on.

        Returns:
            The keys' SQL, empty for a model without a primary key.
        """
        meta = model._meta
        if not meta.has_primary_key:
            return []
        return [
            self.get_expression_sql(model, key, editor)
            for key in self.get_primary_key_keys(meta.primary_key_attribute_names)
        ]

    def get_primary_key_keys(self, primary_key_field_names: Sequence[str]) -> list[str | Expression | RawSQLTerm]:
        """The keys of the table's ``PRIMARY KEY`` - the model's primary key fields, or with
        ``sample_by`` the keys of ``order_by`` up to it: ClickHouse samples by a key of the primary key,
        and a primary key is a prefix of the sort.

        Args:
            primary_key_field_names: The fields of the model's primary key.

        Returns:
            Field names and expressions.
        """
        keys: list[str | Expression | RawSQLTerm] = list(primary_key_field_names)
        sample_position = None if self.sample_by is None else self.get_order_by_position(self.sample_by)
        if sample_position is not None and not any(ComparableValues.are_equal(key, self.sample_by) for key in keys):
            keys = list(self.order_by[: sample_position + 1])
        return keys

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

    def with_row_versions_merged(self, queryset: QuerySet[Any]) -> QuerySet[Any]:
        # A key repeats among the versions of its row until a merge - read with FINAL.
        if not self.keeps_row_versions():
            return queryset
        # A method the dialect adds to QuerySet.
        return cast("QuerySet[Any]", queryset.final())  # type: ignore[attr-defined]

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

    def get_options_during_field_changes(self, old_options: TableOptions | None) -> Self:
        # A time to live, a column's codec or a projection reads columns - one that changes is
        # removed before the fields change and set after: ClickHouse refuses a TTL over a Nullable
        # column, so a column turned NOT NULL takes a new TTL only after its change.
        old = old_options if isinstance(old_options, ClickhouseTableOptions) else ClickhouseTableOptions()

        def is_kept(value: Any, old_values: Sequence[Any]) -> bool:
            return any(ComparableValues.are_equal(value, old_value) for old_value in old_values)

        return dataclasses.replace(
            self,
            ttl=self.ttl if ComparableValues.are_equal(self.ttl, old.ttl) else None,
            column_ttls=tuple(item for item in self.column_ttls if is_kept(item, old.column_ttls)),
            column_codecs=tuple(item for item in self.column_codecs if item in old.column_codecs),
            projections=tuple(projection for projection in self.projections if is_kept(projection, old.projections)),
        )

    def get_key_column_names(self, model: type[Model], dialect: Dialect) -> frozenset[str]:
        """The columns the table's keys (``order_by``, ``sample_by``, ``partition_by``, ``sharding_key``)
        and its engine's arguments (a version, a sign) read - a raw SQL key's columns aren't read out of it,
        the server refuses a change of one.

        Args:
            model: The model.
            dialect: The connection's dialect.

        Returns:
            The column names.
        """
        column_names: set[str] = set()
        for key in (*self.order_by, self.sample_by, self.partition_by, self.sharding_key):
            if key is None or isinstance(key, RawSQLTerm):
                continue
            column_names |= DeclaredExpressions.get_column_names(key, model, dialect) or set()
        for argument in ClickhouseTypeNames.get_type_parts(self.engine)[1]:
            identifier = ClickhouseSqlParts.get_identifier(argument.strip())
            if identifier is not None:
                column_names.add(identifier)
        return frozenset(column_names)

    def get_fixed_field_names(self, model: type[Model]) -> frozenset[str]:
        """The fields of the columns ClickHouse doesn't update in place - those its keys read
        (``get_key_column_names()``).

        Args:
            model: The model.

        Returns:
            The field names, the primary key's own left out.
        """
        bucket = ClickhouseTableOptions.fixed_field_names.get_model_bucket(model)
        fixed_field_names = bucket.get(id(self))
        if fixed_field_names is not None:
            return fixed_field_names
        # Local import: the dialect module imports this one.
        from hare.dialects.clickhouse.constants import CLICKHOUSE_DIALECT

        meta = model._meta
        key_column_names = self.get_key_column_names(model, CLICKHOUSE_DIALECT)
        fixed_field_names = frozenset(
            field_name
            for field_name, column in meta.fields_db_projection.items()
            if field_name not in meta.primary_key_attribute_names and column in key_column_names
        )
        bucket[id(self)] = fixed_field_names
        return fixed_field_names

    def raise_if_fixed_fields_written(self, model: type[Model], field_names: Iterable[str]) -> None:
        """Refuses an update writing a field of a key column - a part of a composite primary key too:
        the sorting key begins with the primary key. A primary key of one field is refused by every
        update itself.

        Args:
            model: The model.
            field_names: The fields written.

        Raises:
            UnSupportedError: A field is a column of the table's keys.
        """
        field_names = list(field_names)
        super().raise_if_fixed_fields_written(model, field_names)
        meta = model._meta
        key_names = frozenset(meta.primary_key_attribute_names)
        if len(key_names) < 2:
            return
        written_key_names = [
            name
            for name in field_names
            if (field := meta.fields_map.get(name)) is not None
            and not key_names.isdisjoint(getattr(field, "source_fields", None) or (name,))
        ]
        if written_key_names:
            raise UnSupportedError(
                FIXED_FIELDS_WRITTEN_MESSAGE.format(model=model.__name__, fields=", ".join(written_key_names))
            )

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
