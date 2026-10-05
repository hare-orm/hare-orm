from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, TypeVar

from hare.fields.field import Field
from hare.query.key_columns import KeyColumns
from hare.query.queryset.relations.many_to_many.many_to_many_scope_checks import ManyToManyScopeChecks
from hare.query.queryset.relations.many_to_many.through_rows import ThroughRows
from hare.query.rows.enums import WriteCodecType
from hare.query.rows.native.enums import FieldBucket
from hare.sql.builder.tables.table import Table

TModel = TypeVar("TModel", bound="Model")

if TYPE_CHECKING:
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.models import Model
    from hare.query.queryset.relations.many_to_many_relation import ManyToManyRelation


class LinkedKeys:
    """The keys a many-to-many relation's through table links an instance to, read as the related model
    stores them."""

    @staticmethod
    async def get_linked_pk_values(
        relation: ManyToManyRelation[Any], connection: DatabaseClient
    ) -> frozenset[tuple[Any, ...]]:
        """The DB-ready primary key values of every related row actively linked to ``relation.instance``.

        Args:
            relation: The many-to-many relation of an instance.
            connection: Connection to read the through table on.
        """
        through_table = Table(relation.field.through, schema=relation.field.through_schema)
        query = LinkedKeys.get_links_query(relation, connection, through_table)
        if (
            visible_target_criterion := await ManyToManyScopeChecks.get_visible_links_criterion(
                relation, through_table, connection=connection
            )
        ) is not None:
            query = query.where(visible_target_criterion)
        _, rows = await connection.execute(*ThroughRows.get_statement(relation, query))
        related_meta = relation.model._meta
        related_pk_fields = related_meta.pk_fields if related_meta.has_composite_primary_key else (related_meta.pk,)
        types = connection.dialect.types
        forward_keys = relation.field.forward_keys
        if LinkedKeys.keys_read_as_stored(related_pk_fields, connection):
            return frozenset(
                values
                for row in rows
                if None not in (values := tuple(row[column_name] for column_name in forward_keys))
            )
        linked_pk_values: list[tuple[Any, ...]] = []
        for row in rows:
            if any(row[column_name] is None for column_name in relation.field.forward_keys):
                continue
            # Read as the field reads it, then written back - a dialect storing a value otherwise
            # (a UUID in 16 bytes) returns the stored form.
            linked_pk_values.append(
                tuple(
                    types.get_db_value(pk_field, types.get_python_value(pk_field, row[column_name]), relation.model)
                    for pk_field, column_name in zip(related_pk_fields, relation.field.forward_keys, strict=True)
                )
            )
        return frozenset(linked_pk_values)

    @staticmethod
    def keys_read_as_stored(key_fields: Sequence[Field[Any]], connection: DatabaseClient) -> bool:
        """Whether every key field takes the value the driver returns as it is, and writes it unchanged -
        a linked key needs no conversion to compare with a member's.

        Args:
            key_fields: The related model's key fields.
            connection: The connection the keys are read on.

        Returns:
            True when no key field converts its values.
        """
        # Local import: the row codecs import the models, which import this module.
        from hare.query.rows.native.field_codecs import FieldCodecs
        from hare.query.rows.native.hydration_layout import HydrationLayout

        types = connection.dialect.types
        return all(
            HydrationLayout.get_field_reading(field, types, connection.native_python_types)[0] == FieldBucket.NATIVE
            and FieldCodecs.get_write_specification(field, types, None)[0] == WriteCodecType.SCALAR
            for field in key_fields
        )

    @staticmethod
    def get_links_query(relation: ManyToManyRelation[Any], connection: DatabaseClient, through_table: Table) -> Any:
        """The query of the related keys of the instance's live links.

        Args:
            relation: The many-to-many relation of an instance.
            connection: Connection the through table is read on.
            through_table: The through table.

        Returns:
            The query, selecting the related key columns.
        """
        backward_columns = [through_table[column_name] for column_name in relation.field.backward_keys]
        backward_key_values = KeyColumns.get_db_values(
            type(relation.instance)._meta, relation.instance, connection.dialect.types
        )
        query = (
            connection.query_class.from_(through_table)
            .where(KeyColumns.row_equality(backward_columns, backward_key_values))
            .select(*relation.field.forward_keys)
        )
        through_model = relation.field.through_model_class
        soft_delete_field = through_model._meta.soft_delete_field if through_model is not None else None
        if through_model is not None and soft_delete_field is not None:
            soft_delete_column = through_model._meta.fields_map[soft_delete_field].source_field or soft_delete_field
            query = query.where(through_table[soft_delete_column].isnull())
        return query

    @staticmethod
    async def read_linked_keys(relation: ManyToManyRelation[Any]) -> list[Any]:
        """The primary keys of the related rows the instance's live links point at, read off the
        through table on its own connection - the related model's own scope left to its query.

        Args:
            relation: The many-to-many relation of an instance.

        Returns:
            The keys - a tuple each for a composite primary key.
        """
        connection = ThroughRows.through_table_connection(relation, for_write=False)
        through_table = Table(relation.field.through, schema=relation.field.through_schema)
        query = LinkedKeys.get_links_query(relation, connection, through_table)
        _, rows = await connection.execute(*ThroughRows.get_statement(relation, query))
        related_meta = relation.model._meta
        related_pk_fields = related_meta.pk_fields if related_meta.has_composite_primary_key else (related_meta.pk,)
        types = connection.dialect.types
        keys: list[Any] = []
        for row in rows:
            if any(row[column_name] is None for column_name in relation.field.forward_keys):
                continue
            key = tuple(
                types.get_python_value(pk_field, row[column_name])
                for pk_field, column_name in zip(related_pk_fields, relation.field.forward_keys, strict=True)
            )
            keys.append(key if len(key) > 1 else key[0])
        return keys
