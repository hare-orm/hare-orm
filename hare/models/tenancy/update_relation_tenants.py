from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hare.fields.relations.fields.foreign_key_field_instance import ForeignKeyFieldInstance
from hare.fields.relations.fields.relational_field import RelationalField
from hare.models.tenancy.tenancy import Tenancy
from hare.query.expressions import Expression, Value
from hare.sql.functions.cast import Cast
from hare.sql.terms.term import Term

if TYPE_CHECKING:
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.models.model_binding import ModelBinding
    from hare.query.execution.prepared_write import PreparedWrite
    from hare.query.scopes.row_visibility import RowVisibility
    from hare.query.statements.write.update_query import UpdateQuery


class UpdateRelationTenants:
    """Checks constant and computed relation keys against tenant boundaries before an update."""

    @staticmethod
    async def check_relation_tenants(
        query: UpdateQuery, connection: DatabaseClient, visibility: RowVisibility, prepared: PreparedWrite
    ) -> None:
        """Rejects an update pointing a relation at a row its model's tenant scope doesn't show.

        Args:
            query: The update, with its relation assignments and matching rows.
            connection: The execution's connection.
            visibility: The visibility the execution resolved.
            prepared: The assignments of this execution.

        Raises:
            QueryError: A relation value names another tenant's row.
        """
        model_binding: ModelBinding = query._binding  # type: ignore[assignment]
        if visibility.all_tenants or not Tenancy.has_tenant_scoped_relations(model_binding):
            return

        # A relation computed by the update keeps no plan - read off this query's built statement.
        await UpdateRelationTenants.check_computed_relation_tenants(query)
        row: dict[str, Any] = {}
        for key, value in query.update_kwargs.items():
            value = UpdateRelationTenants.get_constant_value(value)
            if isinstance(value, (Expression, Term)):
                continue
            field_object = model_binding.fields_map.get(key)
            if isinstance(field_object, ForeignKeyFieldInstance):
                key_values = (prepared.values[key, index].value for index in range(len(field_object.source_fields)))
                row.update(zip(field_object.source_fields, key_values, strict=True))
            else:
                row[key] = value
        await Tenancy.check_relation_targets(model_binding, [row], connection, set(query.update_kwargs))

    @staticmethod
    def get_constant_value(value: Any) -> Any:
        """The value a ``Value(...)`` stands for - a constant the update writes as given - else the
        value itself.

        Args:
            value: An update value.

        Returns:
            The value.
        """
        return value.value if type(value) is Value else value

    @staticmethod
    def get_tenant_checked_computed_relations(query: UpdateQuery) -> list[RelationalField[Any]]:
        """The tenant-scoped forward relations whose new key the update's values don't give alone - set from an
        expression, or one column of a key of several set while the others keep the row's.

        Returns:
            The relations.
        """
        meta: ModelBinding = query._binding  # type: ignore[assignment]
        relations: list[RelationalField[Any]] = []
        for relation_name in sorted(meta.forward_key_relation_names):
            relation: RelationalField[Any] = meta.fields_map[relation_name]  # type: ignore[assignment]
            source_fields = relation.source_fields
            set_names = [name for name in (relation_name, *source_fields) if name in query.update_kwargs]
            if not set_names:
                continue
            sets_expression = any(
                isinstance(UpdateRelationTenants.get_constant_value(query.update_kwargs[name]), (Expression, Term))
                for name in set_names
            )
            sets_part_of_key = relation_name not in set_names and len(set_names) < len(source_fields)
            if (sets_expression or sets_part_of_key) and relation.get_related_binding().tenant_field:
                relations.append(relation)
        return relations

    @staticmethod
    async def check_computed_relation_tenants(query: UpdateQuery) -> None:
        """Rejects an update leaving a relation pointing at a row its model's tenant scope doesn't
        show, where the new key is computed - read by a ``SELECT`` of what the update writes into the
        key columns, for the rows it matches, before it runs.

        Raises:
            QueryError: A row's new key names another tenant's row.
        """
        relations = UpdateRelationTenants.get_tenant_checked_computed_relations(query)
        if not relations:
            return
        meta: ModelBinding = query._binding  # type: ignore[assignment]
        table = meta.basetable
        assigned_terms = {field.name: term for field, term in query.query._updates}
        key_field_names = [source_field for relation in relations for source_field in relation.source_fields]
        dialect = query._connection.dialect
        selected_terms: list[Term] = []
        for key_field_name in key_field_names:
            column = meta.fields_db_projection[key_field_name]
            assigned_term = assigned_terms.get(column)
            if assigned_term is None:
                selected_terms.append(table[column].as_(key_field_name))
                continue
            # A value bound alone in the select list has no type of its own - the column's is given.
            column_type = meta.fields_map[key_field_name].get_column_type(dialect)
            selected_terms.append(Cast(assigned_term, column_type).as_(key_field_name))
        values_query = query._connection.query_class.from_(table).select(*selected_terms)
        # The conditions and the values may read the CTEs of the update.
        values_query._with = list(query.query._with)
        if query.query._wheres is not None:
            values_query = values_query.where(query.query._wheres)
        _row_count, raw_rows = await query._connection._execute_built(
            *values_query.get_parameterized_sql(), returns_rows=True
        )
        types = query._connection.dialect.types
        readers = tuple((name, types.get_python_reader(meta.fields_map[name])) for name in key_field_names)
        rows = []
        for raw_row in raw_rows:
            row = {}
            for name, read in readers:
                value = raw_row[name]
                row[name] = None if value is None else read(value)
            rows.append(row)
        await Tenancy.check_relation_targets(
            meta, rows, query._connection, {relation.model_field_name for relation in relations}
        )
