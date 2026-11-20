from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hare.ddl.conditions.constraint_condition import ConstraintCondition
from hare.ddl.constraints.unique_constraint import UniqueConstraint
from hare.ddl.indexes.index import Index
from hare.exceptions import ConfigurationError, UnSupportedError
from hare.fields.generated_field import GeneratedField
from hare.models.uniqueness.declared_uniqueness import DeclaredUniqueness
from hare.query.expressions import Expression, Q

if TYPE_CHECKING:
    from hare.core.connections.connection_handler import ConnectionHandler
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.models.model_binding import ModelBinding


class ModelConnectionChecks:
    """The refusal of a model whose writes its connection's database can't make - checked when the
    model is bound to the connection, before any of its rows is written."""

    @staticmethod
    def check_model_writes(binding: ModelBinding, connection: DatabaseClient) -> None:
        """Refuses a model whose primary key the database generates on a connection without
        generated keys, a uniqueness or a constrained relation on one keeping none, a ``UniqueConstraint``
        option the database has no clause for, and a soft-deleted model on one that doesn't update stored
        rows.

        Args:
            binding: The model, bound to its connection.
            connection: The configured client whose database capabilities are checked.

        Raises:
            ConfigurationError: The model's connection can't make one of its writes.
        """
        features = connection.features
        ModelConnectionChecks.raise_if_columns_unsupported(binding, connection)
        if binding.generated_pk_field_name is not None and not features.supports_generated_keys:
            raise ConfigurationError(
                f'"{binding.full_name}.{binding.generated_pk_field_name}" is a primary key the database generates, '
                f"but the {connection.dialect.name} database of connection {binding.default_connection!r} generates "
                "no keys - give the field generated=False and a default (e.g. default=uuid.uuid7 on a UUIDField), "
                "or set the key on every row written"
            )
        ModelConnectionChecks.raise_if_constraints_unsupported(binding, connection)
        if binding.optimistic_lock_field is not None and not features.reports_written_row_count:
            raise ConfigurationError(
                f"{binding.full_name!r} has Meta.optimistic_lock_field, but the {connection.dialect.name} "
                f"database of connection {binding.default_connection!r} reports no count of the rows a write "
                "changed - a stale version would go unnoticed"
            )
        if features.supports_unique_constraints:
            ModelConnectionChecks.raise_if_unique_constraint_options_unsupported(binding, connection)
        ModelConnectionChecks.raise_if_index_key_orders_unsupported(binding, connection)
        ModelConnectionChecks.raise_if_ddl_expressions_unsupported(binding, connection)
        if binding.soft_delete_field is not None and not features.supports_row_updates:
            raise ConfigurationError(
                f"{binding.full_name!r} is soft-deleted (Meta.soft_delete_field), but the {connection.dialect.name} "
                f"database of connection {binding.default_connection!r} doesn't update stored rows"
            )

    @staticmethod
    def raise_if_constraints_unsupported(binding: ModelBinding, connection: DatabaseClient) -> None:
        """Refuses a uniqueness on a database keeping none, a relation of ``db_constraint=True`` on one
        keeping no foreign keys, and one to a model of another schema on one whose foreign keys reference
        only their own schema - the database would keep none of them.

        Args:
            binding: The model, bound to its connection.
            connection: The configured client whose database capabilities are checked.

        Raises:
            ConfigurationError: The model declares such a uniqueness or relation.
        """
        features = connection.features
        database = f"the {connection.dialect.name} database of connection {binding.default_connection!r}"
        if not features.supports_unique_constraints:
            relation_names = binding.forward_key_relation_names
            declarations = [
                f"{field_name} (unique=True)"
                for field_name, field in binding.fields_map.items()
                if getattr(field, "unique", False)
                and not getattr(field, "pk", False)
                # The key field of a unique relation - the relation itself is named.
                and field.reference is None
                and (field_name in binding.fields_db_projection or field_name in relation_names)
            ]
            declarations.extend(
                f"{type(schema_object).__name__}({schema_object.name or ', '.join(schema_object.fields)})"
                for schema_object in (*binding.constraints, *binding.indexes)
                if isinstance(schema_object, (UniqueConstraint, Index))
                and DeclaredUniqueness.is_declared_by(schema_object)
            )
            if declarations:
                raise ConfigurationError(
                    f"{binding.full_name!r} declares {', '.join(declarations)}, but {database} keeps nothing unique"
                )
        if not features.supports_foreign_keys:
            # The automatic through table of a many-to-many relation, declared on one side, holds a foreign key
            # to each.
            constrained_names = [
                field_name
                for field_name in (
                    *binding.foreign_key_fields,
                    *binding.one_to_one_fields,
                    *binding.many_to_many_fields,
                )
                if getattr(field := binding.fields_map[field_name], "db_constraint", False)
                and getattr(field, "through_model", None) is None
                and not getattr(field, "_generated", False)
            ]
            if constrained_names:
                raise ConfigurationError(
                    f"{binding.full_name!r} declares the relations {', '.join(constrained_names)} with "
                    f"db_constraint=True, but {database} keeps no foreign key - declare db_constraint=False"
                )
        elif not features.references_other_schemas:
            crossing_names = [
                field_name
                for field_name in (
                    *binding.foreign_key_fields,
                    *binding.one_to_one_fields,
                    *binding.many_to_many_fields,
                )
                if getattr(field := binding.fields_map[field_name], "db_constraint", False)
                and getattr(field, "through_model", None) is None
                and not getattr(field, "_generated", False)
                and connection.dialect.literals.get_schema_key(field.get_related_binding().schema)  # type: ignore[attr-defined]
                != connection.dialect.literals.get_schema_key(binding.schema)
            ]
            if crossing_names:
                raise ConfigurationError(
                    f"{binding.full_name!r} declares the relations {', '.join(crossing_names)} to models of another "
                    f"schema with db_constraint=True, but the foreign keys of {database} reference only tables "
                    "of their own schema - declare db_constraint=False"
                )

    @staticmethod
    def raise_if_columns_unsupported(binding: ModelBinding, connection: DatabaseClient) -> None:
        """Refuses a field whose column the database has no type of, or whose declaration it refuses (a
        NULL its type can't hold) - before a migration of it is written, which no migrate could apply.

        Args:
            binding: The model, bound to its connection.
            connection: The configured client whose database capabilities are checked.

        Raises:
            UnSupportedError: A field of the model is such a field.
        """
        dialect = connection.dialect
        for field_name in binding.fields_db_projection:
            field = binding.fields_map[field_name]
            try:
                field.check_dialect_supported(dialect)
                field.get_column_type(dialect)
            except UnSupportedError as error:
                raise UnSupportedError(
                    f'"{binding.full_name}.{field_name}" has no column on the {dialect.name} database of connection '
                    f"{binding.default_connection!r}: {error}"
                ) from error

    @staticmethod
    def raise_if_index_key_orders_unsupported(binding: ModelBinding, connection: DatabaseClient) -> None:
        """Refuses an index of a key direction or NULL placement the database's indexes don't keep -
        before a migration of it is written, which no migrate could apply.

        Args:
            binding: The model, bound to its connection.
            connection: The configured client whose database capabilities are checked.

        Raises:
            ConfigurationError: An index sets such a key order.
        """
        dialect = connection.dialect
        for index in binding.indexes:
            if not isinstance(index, Index):
                continue
            try:
                index.raise_if_unsupported(dialect)
            except UnSupportedError as error:
                raise ConfigurationError(
                    f"{binding.full_name!r} declares the index {index.name or ', '.join(index.fields)}: {error}"
                ) from error

    @staticmethod
    def raise_if_names_differ_only_in_case(models: list[ModelBinding], connections: ConnectionHandler) -> None:
        """Refuses names a database ignoring the case of letters (``Features.names_ignore_letter_case``)
        takes for one within a model's columns. Table and object namespaces are checked
        during relation binding by ModelNameChecks.

        Args:
            models: The registered models, bound to their connections.
            connections: Clients by the alias configured for each model.

        Raises:
            ConfigurationError: Two names differ only in the case of their letters.
        """
        seen_names: dict[tuple[Any, ...], tuple[str, str]] = {}
        for binding in models:
            connection = connections.get_main(binding.default_connection)  # type: ignore[arg-type]
            if not connection.features.names_ignore_letter_case:
                continue
            dialect = connection.dialect
            literals = dialect.literals
            alias = binding.default_connection
            schema_key = literals.get_schema_key(binding.schema)
            for field_name, column_name in binding.fields_db_projection.items():
                table_key = literals.get_identifier_key(binding.db_table)
                owner = f"{binding.full_name}.{field_name}"
                column_key = literals.get_identifier_key(column_name)
                key = (dialect, alias, schema_key, table_key, column_key)
                seen_name, seen_owner = seen_names.setdefault(key, (column_name, owner))
                if seen_name != column_name:
                    raise ConfigurationError(
                        f"{seen_owner} and {owner} declare the columns {seen_name!r} and {column_name!r} - the "
                        f"{dialect} database of connection {alias!r} takes names differing only in the case of "
                        "their letters for one name: rename one of them"
                    )

    @staticmethod
    def raise_if_ddl_expressions_unsupported(binding: ModelBinding, connection: DatabaseClient) -> None:
        """Refuses an expression of an index key, of a constraint's or a partial index's condition and of a
        generated column that the database can't keep in its DDL - a function it only has on hare's own
        connections - before a migration of it is written, which no migrate
        could apply.

        Args:
            binding: The model, bound to its connection.
            connection: The configured client whose database capabilities are checked.

        Raises:
            UnSupportedError: A declaration of the model holds such an expression.
        """
        client = connection
        dialect = client.dialect
        conditions = ConstraintCondition.get_table_conditions(binding)
        try:
            for index in binding.indexes:
                if isinstance(index, Index) and index.declared_expressions:
                    index.get_key_sqls(binding, dialect)
            for condition in conditions:
                if isinstance(condition, Q):
                    ConstraintCondition.get_sql(condition, binding, client)
            for field in binding.fields_map.values():
                if isinstance(field, GeneratedField) and isinstance(field.expression, Expression):
                    field.get_generated_sql(dialect)
        except UnSupportedError as error:
            raise UnSupportedError(
                f"{binding.full_name!r} can't be kept by the {dialect.name} database of connection "
                f"{binding.default_connection!r}: {error}"
            ) from error

    @staticmethod
    def raise_if_unique_constraint_options_unsupported(binding: ModelBinding, connection: DatabaseClient) -> None:
        """Refuses a ``UniqueConstraint`` of ``nulls_distinct`` or ``without_overlaps`` on a database
        without the clause - before a migration of it is written, which no migrate could apply.

        Args:
            binding: The model, bound to its connection.
            connection: The configured client whose database capabilities are checked.

        Raises:
            ConfigurationError: A constraint sets such an option.
        """
        features = connection.features
        for constraint in binding.constraints:
            if not isinstance(constraint, UniqueConstraint):
                continue
            option = constraint.get_unsupported_option(features)
            if option is not None:
                constraint_name = constraint.name or ", ".join(constraint.fields)
                raise ConfigurationError(
                    f"{binding.full_name!r} declares UniqueConstraint({constraint_name}) with {option}, which the "
                    f"{connection.dialect.name} database of connection {binding.default_connection!r} doesn't "
                    "support"
                )
