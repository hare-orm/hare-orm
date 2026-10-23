from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, TypeVar, cast

from hare.fields.field import Field
from hare.instrumentation.enums import RowOperation
from hare.models.instances.instance_connections import InstanceConnections
from hare.models.write.write_steps import WriteSteps
from hare.sql.terms.parameters.query_parameters import QueryParameters

TModel = TypeVar("TModel", bound="Model")

if TYPE_CHECKING:
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.models import Model
    from hare.query.queryset.relations.many_to_many_relation import ManyToManyRelation
    from hare.sql.terms.field import Field as SqlField


class ThroughRows:
    """The rows of a many-to-many relation's through table, what every write of them shares: the connection
    of the model declaring the field, the statement of a query of them with its sensitive values hidden,
    and the report of the links changed."""

    @staticmethod
    def through_table_connection(relation: ManyToManyRelation[Any], for_write: bool) -> DatabaseClient:
        """The connection the through table lives on: that of the model declaring the forward side of
        the field - the instance's model from the forward side, the related model from the backward
        one.

        Args:
            relation: The many-to-many relation of an instance.
            for_write: Whether the connection is one to write through.
        """
        owning_model = relation.model if relation.field._generated else type(relation.instance)
        return InstanceConnections.get_connection_for_instance(relation.instance, for_write, model=owning_model)

    @staticmethod
    def holds_sensitive_keys(relation: ManyToManyRelation[Any]) -> bool:
        """Whether either side of a many-to-many relation is keyed by a ``sensitive=True`` field - its
        through rows hold those values.

        Args:
            relation: The many-to-many relation of an instance.

        Returns:
            True when a primary key field of either model is sensitive.
        """
        return any(
            field.sensitive
            for meta in (type(relation.instance)._meta, relation.model._meta)
            for field in (meta.pk_fields if meta.has_composite_primary_key else (meta.pk,))
        )

    @staticmethod
    def holds_sensitive_values(relation: ManyToManyRelation[Any], through_defaults: dict[str, Any] | None) -> bool:
        """Whether the through rows written hold a ``sensitive=True`` value - a sensitive key of either model,
        or a sensitive field of the through model given in ``through_defaults``.

        Args:
            relation: The many-to-many relation of an instance.
            through_defaults: The values the caller gives the through rows' own fields, None for none.

        Returns:
            True when one is written.
        """
        if ThroughRows.holds_sensitive_keys(relation):
            return True
        through_model = relation.field.through_model_class
        if not through_defaults or through_model is None:
            return False
        return not through_model._meta.sensitive_fields.isdisjoint(through_defaults)

    @staticmethod
    def hide_values(relation: ManyToManyRelation[Any], values: list[Any]) -> list[Any]:
        """The parameters of a through-table statement, none of them shown when the relation holds
        sensitive keys (``QueryParameters``).

        Args:
            relation: The many-to-many relation of an instance.
            values: The parameters.

        Returns:
            The parameters.
        """
        if ThroughRows.holds_sensitive_keys(relation):
            return QueryParameters(values, range(len(values)))
        return values

    @staticmethod
    def get_statement(relation: ManyToManyRelation[Any], query: Any) -> tuple[str, list[Any]]:
        """The SQL and parameters of a through-table statement - see ``hide_values()``.

        Args:
            relation: The many-to-many relation of an instance.
            query: The statement.

        Returns:
            The SQL and its parameters.
        """
        sql, values = query.get_parameterized_sql()
        return sql, ThroughRows.hide_values(relation, values)

    @staticmethod
    def get_column_fields(relation: ManyToManyRelation[Any], columns: list[SqlField]) -> list[Field[Any]] | None:
        """The field of each through-table column - the through model's own, or the key fields of the two
        models an automatic through table links.

        Args:
            relation: The many-to-many relation of an instance.
            columns: The through table's columns.

        Returns:
            The fields, one per column; None when a column is of no field.
        """
        through_model = relation.field.through_model_class
        if through_model is not None:
            through_meta = through_model._meta
            fields_by_column = {
                through_meta.fields_db_projection.get(name, field.source_field or name): field
                for name, field in through_meta.fields_map.items()
            }
        else:
            related_meta = relation.model._meta
            owner_meta = type(relation.instance)._meta
            related_key_fields = (
                related_meta.pk_fields if related_meta.has_composite_primary_key else (related_meta.pk,)
            )
            owner_key_fields = owner_meta.pk_fields if owner_meta.has_composite_primary_key else (owner_meta.pk,)
            fields_by_column = {
                **dict(zip(relation.field.forward_keys, related_key_fields, strict=True)),
                **dict(zip(relation.field.backward_keys, owner_key_fields, strict=True)),
            }
        column_fields = [fields_by_column.get(column.name) for column in columns]
        if any(field is None for field in column_fields):
            return None
        return cast("list[Field[Any]]", column_fields)

    @staticmethod
    async def report_links(
        relation: ManyToManyRelation[Any],
        connection: DatabaseClient,
        related_instances: Sequence[Any] | None,
        operation: RowOperation,
        *,
        reports_through_rows: bool = True,
    ) -> None:
        """Reports links added or removed (``ChangeEvents``): the owner's and the related rows'
        relation changed, and the rows of a through model were inserted or deleted.

        Args:
            relation: The many-to-many relation of an instance.
            connection: The connection the links were written on.
            related_instances: The related rows, None for every one.
            operation: ``INSERT`` for added links, ``DELETE`` for removed ones.
            reports_through_rows: Whether the rows of a through model are reported - not for links restored from
                a soft delete alone, whose restore reports them.
        """
        await WriteSteps.report(
            connection,
            type(relation.instance),
            RowOperation.UPDATE,
            instances=[relation.instance],
            fields=[relation.field.model_field_name],
        )
        related_fields = [relation.field.related_name] if relation.field.related_name else None
        if related_instances is None:
            await WriteSteps.report(connection, relation.model, RowOperation.UPDATE, fields=related_fields)
        else:
            await WriteSteps.report(
                connection,
                relation.model,
                RowOperation.UPDATE,
                instances=list(related_instances),
                fields=related_fields,
            )
        through_model = relation.field.through_model_class
        if through_model is not None and reports_through_rows:
            if operation is RowOperation.DELETE and through_model._meta.soft_delete_field:
                await WriteSteps.report(
                    connection, through_model, RowOperation.UPDATE, fields=[through_model._meta.soft_delete_field]
                )
            else:
                await WriteSteps.report(connection, through_model, operation)
