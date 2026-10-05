from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

from hare.dialects.sqlite.constants import SPATIALITE_INDEX_RESULT_COLUMN, SPATIALITE_INDEX_TYPE
from hare.dialects.sqlite.indexes.constants import (
    SPATIALITE_CREATE_INDEX_SQL,
    SPATIALITE_DIMENSION_NAMES,
    SPATIALITE_DISABLE_INDEX_SQL,
    SPATIALITE_DROP_INDEX_TABLE_SQL,
    SPATIALITE_ENABLED_INDEX_CONDITION_SQL,
    SPATIALITE_INDEX_TABLE_NAME,
    SPATIALITE_REGISTER_COLUMN_SQL,
    SPATIALITE_REGISTERED_COLUMN_SQL,
    SPATIALITE_UNREGISTER_COLUMN_SQL,
)
from hare.dialects.sqlite.indexes.own_table_index import OwnTableIndex
from hare.dialects.sqlite.sqlite_dialect import SqliteDialect
from hare.exceptions import ConfigurationError, UnSupportedError
from hare.gis.fields.geometry_field import GeometryField
from hare.gis.spatial_features import SpatialFeatures

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.dialects.base.dialect import Dialect
    from hare.dialects.base.features import Features
    from hare.dialects.base.schema.base_schema_editor import BaseSchemaEditor
    from hare.models import Model


class SpatialiteIndex(OwnTableIndex):
    """SpatiaLite's spatial index of a geometry field on SQLite - an R*Tree of the bounding boxes of
    the column's geometries in a table SpatiaLite names ``idx_<table>_<column>``, kept in step by
    SpatiaLite's triggers and filled from the rows already there when it is created. The column is
    registered in the spatial metadata with the field's geometry type, SRID and dimensions first -
    SpatiaLite's triggers refuse any other geometry from then on.

    A spatial lookup on the field finds the rows whose bounding box meets the other geometry's
    through the index before testing them - ``__dwithin`` of a geometry through the box grown by
    the distance; ``__disjoint``, ``__relate`` and ``__dwithin`` of a geography test every row.

    The index needs SpatiaLite's spatial metadata holding the field's SRID
    (``features.supports_spatial_index``, ``features.spatial_reference_ids``); another dialect
    raises ``UnSupportedError`` - the PostgreSQL one is ``GistIndex``. It takes no name: its table
    is named by SpatiaLite.

    Args:
        fields: The one geometry field.

    Raises:
        ConfigurationError: Not exactly one field, or a descending key.
    """

    INDEX_TYPE = SPATIALITE_INDEX_TYPE

    def __init__(self, *, fields: Sequence[str]) -> None:
        if isinstance(fields, str) or len(fields) != 1:
            raise ConfigurationError(f"SpatialiteIndex fields must name exactly one geometry field, got {fields!r}")
        super().__init__(fields=tuple(fields))
        if any(self.field_orders):
            raise ConfigurationError("SpatialiteIndex has no key order - its field is named without '-'")

    @classmethod
    def get_covering(cls, model: type[Model], field_name: str) -> SpatialiteIndex | None:
        """The model's spatial index of a field.

        Args:
            model: The model.
            field_name: The field.

        Returns:
            The index, None when the field has none.
        """
        for index in model._meta.indexes:
            if isinstance(index, cls) and index.fields[0] == field_name:
                return index
        return None

    def get_index_table_name(self, table_name: str, column_names: Sequence[str]) -> str:
        """The R*Tree table - ``idx_<table>_<column>``."""
        return SPATIALITE_INDEX_TABLE_NAME.format(table=table_name, column=column_names[0])

    def index_name(self, schema_editor: BaseSchemaEditor, model: type[Model]) -> str:
        return self.get_table_name(model)

    def raise_if_unsupported(self, dialect: Dialect) -> None:
        """Rejects the index on a dialect other than SQLite.

        Raises:
            UnSupportedError: The dialect isn't SQLite.
        """
        self.raise_if_other_dialect(dialect)
        super().raise_if_unsupported(dialect)

    def raise_if_not_droppable(self, features: Features, dialect: Dialect) -> None:
        """Rejects dropping the index where it can't exist.

        Raises:
            UnSupportedError: The dialect isn't SQLite, or the connection has no spatial index.
        """
        self.raise_if_other_dialect(dialect)
        self.raise_if_missing_feature(features, "this connection")

    def raise_if_other_dialect(self, dialect: Dialect) -> None:
        """Rejects the index on a dialect other than SQLite - before any SQL, so the model or the
        migration names the other database's index instead.

        Args:
            dialect: The dialect of the database the DDL runs on.

        Raises:
            UnSupportedError: The dialect isn't SQLite.
        """
        if not isinstance(dialect, SqliteDialect):
            raise UnSupportedError(
                f"SpatialiteIndex(fields={list(self.fields)!r}) is an index of SQLite's SpatiaLite - the {dialect} "
                "dialect has none: declare an index of that database in the model, or change the migration"
            )

    def raise_if_missing_feature(self, features: Features, database_name: str) -> None:
        """Rejects the index where the features have no spatial index.

        Args:
            features: The features of the connection.
            database_name: What the features are of, for the message.

        Raises:
            UnSupportedError: No ``features.supports_spatial_index``.
        """
        if not features.supports_spatial_index:
            raise UnSupportedError(
                f"SpatialiteIndex(fields={list(self.fields)!r}) can't be created on {database_name}: it needs "
                "features.supports_spatial_index - SpatiaLite loaded with its spatial metadata"
            )

    def get_geometry_field(self, model: type[Model]) -> GeometryField:
        """The indexed field.

        Args:
            model: The indexed model.

        Returns:
            The field.

        Raises:
            ConfigurationError: The field isn't a ``GeometryField``.
        """
        field = model._meta.fields_map.get(self.fields[0])
        if not isinstance(field, GeometryField):
            raise ConfigurationError(
                f"{model.__name__}: SpatialiteIndex indexes a GeometryField - {self.fields[0]!r} isn't one"
            )
        return field

    def get_extra(self, model: type[Model], client: DatabaseClient) -> str:
        """Rejects the index where it can't be created - a plain ``CREATE INDEX`` is never written
        for it.

        Raises:
            UnSupportedError: The connection has no spatial index.
        """
        self.raise_if_missing_feature(client.features, repr(client.connection_alias))
        return ""

    def get_create_sqls(self, schema_editor: BaseSchemaEditor, model: type[Model], safe: bool) -> list[str]:
        """Returns the statements registering the column and creating its R*Tree - each answers 1
        in ``SPATIALITE_INDEX_RESULT_COLUMN`` when it did its part, 0 when SpatiaLite refused it.

        Raises:
            UnSupportedError: The connection isn't SQLite's, has no spatial index, or its spatial
                metadata hasn't the field's SRID.
            ConfigurationError: The field isn't a geometry, or the model has no integer primary key.
        """
        client = schema_editor.client
        self.raise_if_other_dialect(client.dialect)
        self.raise_if_missing_feature(client.features, repr(client.connection_alias))
        field = self.get_geometry_field(model)
        self.get_row_key_column(model)
        SpatialFeatures.raise_if_unknown_reference_system(
            client, f"SpatialiteIndex(fields={list(self.fields)!r}) of {model.__name__}", field.srid
        )
        literals = client.dialect.literals
        table_sql = literals.get_string_literal_sql(model._meta.db_table)
        column_sql = literals.get_string_literal_sql(model._meta.get_column_names(self.fields)[0])
        result_sql = schema_editor.quote(SPATIALITE_INDEX_RESULT_COLUMN)
        register_sql = SPATIALITE_REGISTER_COLUMN_SQL.format(
            table=table_sql,
            column=column_sql,
            srid=field.srid,
            geometry_type=literals.get_string_literal_sql(field.get_geometry_type().value),
            dimensions=literals.get_string_literal_sql(SPATIALITE_DIMENSION_NAMES[field.dimensions]),
            result=result_sql,
        )
        create_sql = SPATIALITE_CREATE_INDEX_SQL.format(table=table_sql, column=column_sql, result=result_sql)
        if safe:
            registered_sql = SPATIALITE_REGISTERED_COLUMN_SQL.format(
                table=table_sql, column=column_sql, spatial_index=""
            )
            indexed_sql = SPATIALITE_REGISTERED_COLUMN_SQL.format(
                table=table_sql, column=column_sql, spatial_index=SPATIALITE_ENABLED_INDEX_CONDITION_SQL
            )
            register_sql = f"{register_sql} WHERE NOT EXISTS ({registered_sql})"
            create_sql = f"{create_sql} WHERE NOT EXISTS ({indexed_sql})"
        return [f"{register_sql};", f"{create_sql};"]

    def get_drop_sqls(
        self, schema_editor: BaseSchemaEditor, table_name: str, column_names: Sequence[str]
    ) -> list[str]:
        """Returns the statements dropping the R*Tree and its triggers and unregistering the column,
        each only when it exists."""
        literals = schema_editor.client.dialect.literals
        table_sql = literals.get_string_literal_sql(table_name)
        column_sql = literals.get_string_literal_sql(column_names[0])
        registered_sql = SPATIALITE_REGISTERED_COLUMN_SQL.format(table=table_sql, column=column_sql, spatial_index="")
        indexed_sql = SPATIALITE_REGISTERED_COLUMN_SQL.format(
            table=table_sql, column=column_sql, spatial_index=SPATIALITE_ENABLED_INDEX_CONDITION_SQL
        )
        index_table_sql = schema_editor.quote(self.get_index_table_name(table_name, column_names))
        return [
            f"{SPATIALITE_DISABLE_INDEX_SQL.format(table=table_sql, column=column_sql)} WHERE EXISTS ({indexed_sql});",
            f"{SPATIALITE_DROP_INDEX_TABLE_SQL.format(index_table=index_table_sql)};",
            f"{SPATIALITE_UNREGISTER_COLUMN_SQL.format(table=table_sql, column=column_sql)} "
            f"WHERE EXISTS ({registered_sql});",
        ]
