from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from hare.ddl.conditions.constraint_condition import ConstraintCondition
from hare.ddl.constraints.check_constraint import CheckConstraint
from hare.ddl.constraints.exclusion_constraint import ExclusionConstraint
from hare.ddl.constraints.unique_constraint import UniqueConstraint
from hare.ddl.indexes.index import Index
from hare.ddl.indexes.partial_index import PartialIndex
from hare.exceptions import ConfigurationError, UnSupportedError
from hare.fields.generated_field import GeneratedField
from hare.models.uniqueness.declared_uniqueness import DeclaredUniqueness
from hare.query.expressions import Expression, Q

if TYPE_CHECKING:
    from hare.models import Model


class ModelConnectionChecks:
    """The refusal of a model whose writes its connection's database can't make - checked when the
    model is bound to the connection, before any of its rows is written."""

    @staticmethod
    def check_model_writes(model: type[Model]) -> None:
        """Refuses a model whose primary key the database generates on a connection without
        generated keys, a uniqueness or a constrained relation on one keeping none, a ``UniqueConstraint``
        option the database has no clause for, and a soft-deleted model on one that doesn't update stored
        rows.

        Args:
            model: The model, bound to its connection.

        Raises:
            ConfigurationError: The model's connection can't make one of its writes.
        """
        meta = model._meta
        features = meta.connection.features
        ModelConnectionChecks.raise_if_columns_unsupported(model)
        if meta.generated_pk_field_name is not None and not features.supports_generated_keys:
            raise ConfigurationError(
                f'"{meta.full_name}.{meta.generated_pk_field_name}" is a primary key the database generates, '
                f"but the {meta.connection.dialect.name} database of connection {meta.default_connection!r} generates "
                "no keys - give the field generated=False and a default (e.g. default=uuid.uuid7 on a UUIDField), "
                "or set the key on every row written"
            )
        ModelConnectionChecks.raise_if_constraints_unsupported(model)
        if meta.optimistic_lock_field is not None and not features.reports_written_row_count:
            raise ConfigurationError(
                f"{meta.full_name!r} has Meta.optimistic_lock_field, but the {meta.connection.dialect.name} "
                f"database of connection {meta.default_connection!r} reports no count of the rows a write "
                "changed - a stale version would go unnoticed"
            )
        if features.supports_unique_constraints:
            ModelConnectionChecks.raise_if_unique_constraint_options_unsupported(model)
        ModelConnectionChecks.raise_if_index_key_orders_unsupported(model)
        ModelConnectionChecks.raise_if_ddl_expressions_unsupported(model)
        if meta.soft_delete_field is not None and not features.supports_row_updates:
            raise ConfigurationError(
                f"{meta.full_name!r} is soft-deleted (Meta.soft_delete_field), but the {meta.connection.dialect.name} "
                f"database of connection {meta.default_connection!r} doesn't update stored rows"
            )

    @staticmethod
    def raise_if_constraints_unsupported(model: type[Model]) -> None:
        """Refuses a uniqueness on a database keeping none, a relation of ``db_constraint=True`` on one
        keeping no foreign keys, and one to a model of another schema on one whose foreign keys reference
        only their own schema - the database would keep none of them.

        Args:
            model: The model, bound to its connection.

        Raises:
            ConfigurationError: The model declares such a uniqueness or relation.
        """
        meta = model._meta
        features = meta.connection.features
        database = f"the {meta.connection.dialect.name} database of connection {meta.default_connection!r}"
        if not features.supports_unique_constraints:
            declarations = [
                f"{field_name} (unique=True)"
                for field_name, field in meta.fields_map.items()
                if getattr(field, "unique", False)
                and not getattr(field, "pk", False)
                # The key field of a unique relation - the relation itself is named.
                and field.reference is None
                and (
                    field_name in meta.fields_db_projection
                    or field_name in meta.foreign_key_fields | meta.one_to_one_fields
                )
            ]
            declarations.extend(
                f"{type(schema_object).__name__}({schema_object.name or ', '.join(schema_object.fields)})"
                for schema_object in (*meta.constraints, *meta.indexes)
                if isinstance(schema_object, UniqueConstraint | Index)
                and DeclaredUniqueness.is_declared_by(schema_object)
            )
            if declarations:
                raise ConfigurationError(
                    f"{meta.full_name!r} declares {', '.join(declarations)}, but {database} keeps nothing unique"
                )
        if not features.supports_foreign_keys:
            # The automatic through table of a many-to-many relation, declared on one side, holds a foreign key
            # to each.
            constrained_names = [
                field_name
                for field_name in (*meta.foreign_key_fields, *meta.one_to_one_fields, *meta.many_to_many_fields)
                if getattr(field := meta.fields_map[field_name], "db_constraint", False)
                and getattr(field, "through_model", None) is None
                and not getattr(field, "_generated", False)
            ]
            if constrained_names:
                raise ConfigurationError(
                    f"{meta.full_name!r} declares the relations {', '.join(constrained_names)} with "
                    f"db_constraint=True, but {database} keeps no foreign key - declare db_constraint=False"
                )
        elif not features.references_other_schemas:
            crossing_names = [
                field_name
                for field_name in (*meta.foreign_key_fields, *meta.one_to_one_fields, *meta.many_to_many_fields)
                if getattr(field := meta.fields_map[field_name], "db_constraint", False)
                and getattr(field, "through_model", None) is None
                and not getattr(field, "_generated", False)
                and cast("Any", field).related_model._meta.schema != meta.schema
            ]
            if crossing_names:
                raise ConfigurationError(
                    f"{meta.full_name!r} declares the relations {', '.join(crossing_names)} to models of another "
                    f"schema with db_constraint=True, but the foreign keys of {database} reference only tables "
                    "of their own schema - declare db_constraint=False"
                )

    @staticmethod
    def raise_if_columns_unsupported(model: type[Model]) -> None:
        """Refuses a field whose column the database has no type of, or whose declaration it refuses (a
        NULL its type can't hold) - before a migration of it is written, which no migrate could apply.

        Args:
            model: The model, bound to its connection.

        Raises:
            UnSupportedError: A field of the model is such a field.
        """
        meta = model._meta
        dialect = meta.connection.dialect
        for field_name in meta.fields_db_projection:
            field = meta.fields_map[field_name]
            try:
                field.check_dialect_supported(dialect)
                field.get_column_type(dialect)
            except UnSupportedError as error:
                raise UnSupportedError(
                    f'"{meta.full_name}.{field_name}" has no column on the {dialect.name} database of connection '
                    f"{meta.default_connection!r}: {error}"
                ) from error

    @staticmethod
    def raise_if_index_key_orders_unsupported(model: type[Model]) -> None:
        """Refuses an index of a key direction or NULL placement the database's indexes don't keep -
        before a migration of it is written, which no migrate could apply.

        Args:
            model: The model, bound to its connection.

        Raises:
            ConfigurationError: An index sets such a key order.
        """
        meta = model._meta
        dialect = meta.connection.dialect
        for index in meta.indexes:
            if not isinstance(index, Index):
                continue
            try:
                index.raise_if_unsupported(dialect)
            except UnSupportedError as error:
                raise ConfigurationError(
                    f"{meta.full_name!r} declares the index {index.name or ', '.join(index.fields)}: {error}"
                ) from error

    @staticmethod
    def raise_if_names_differ_only_in_case(models: list[type[Model]]) -> None:
        """Refuses names a database ignoring the case of letters (``Features.names_ignore_letter_case``)
        takes for one: two tables of one connection and schema, two columns of one model, two named
        constraints or indexes - the second table or column would be the first one, the second index
        silently not created.

        Args:
            models: The registered models, bound to their connections.

        Raises:
            ConfigurationError: Two names differ only in the case of their letters.
        """
        seen_names: dict[tuple[Any, ...], tuple[str, str]] = {}
        for model in models:
            meta = model._meta
            if not meta.connection.features.names_ignore_letter_case:
                continue
            key = (meta.connection.dialect, meta.default_connection, meta.schema)
            owner = repr(meta.full_name)
            ModelConnectionChecks.note_name(seen_names, (*key, "table"), meta.db_table, owner, "tables")
            for field_name, column_name in meta.fields_db_projection.items():
                ModelConnectionChecks.note_name(
                    seen_names,
                    (*key, "column", meta.db_table),
                    column_name,
                    f"{meta.full_name}.{field_name}",
                    "columns",
                )
            for declaration in (*meta.constraints, *meta.indexes):
                name = getattr(declaration, "name", None)
                if name and not isinstance(declaration, CheckConstraint):
                    ModelConnectionChecks.note_name(seen_names, (*key, "index"), name, owner, "constraints or indexes")

    @staticmethod
    def note_name(
        seen_names: dict[tuple[Any, ...], tuple[str, str]], key: tuple[Any, ...], name: str, owner: str, what: str
    ) -> None:
        """Records a name by the dialect's identifier key, refusing a conflicting spelling.

        Args:
            seen_names: The names seen - the name and its owner, by the namespace and identifier key.
            key: The database, the connection and the schema, what the name is of, and its table for a column.
            name: The name.
            owner: What declares it, for the error.
            what: What the names are of, for the error - ``"tables"``.

        Raises:
            ConfigurationError: Another spelling of the name was seen.
        """
        identifier_key = key[0].literals.get_identifier_key(name)
        seen_name, seen_owner = seen_names.setdefault((*key, identifier_key), (name, owner))
        if seen_name != name:
            raise ConfigurationError(
                f"{seen_owner} and {owner} declare the {what} {seen_name!r} and {name!r} - the {key[0]} database "
                f"of connection {key[1]!r} takes names differing only in the case of their letters for one name: "
                "rename one of them"
            )

    @staticmethod
    def raise_if_ddl_expressions_unsupported(model: type[Model]) -> None:
        """Refuses an expression of an index key, of a constraint's or a partial index's condition and of a
        generated column that the database can't keep in its DDL - a function it only has on hare's own
        connections - before a migration of it is written, which no migrate
        could apply.

        Args:
            model: The model, bound to its connection.

        Raises:
            UnSupportedError: A declaration of the model holds such an expression.
        """
        meta = model._meta
        client = meta.connection
        dialect = client.dialect
        conditions: list[Any] = []
        for constraint in meta.constraints:
            if isinstance(constraint, (UniqueConstraint, ExclusionConstraint)):
                conditions.append(constraint.condition)
            elif isinstance(constraint, CheckConstraint):
                conditions.append(constraint.check)
        conditions.extend(index.condition for index in meta.indexes if isinstance(index, PartialIndex))
        try:
            for index in meta.indexes:
                if isinstance(index, Index) and index.expressions:
                    index.get_key_sqls(model, dialect)
            for condition in conditions:
                if isinstance(condition, Q):
                    ConstraintCondition.get_sql(condition, model, client)
            for field in meta.fields_map.values():
                if isinstance(field, GeneratedField) and isinstance(field.expression, Expression):
                    field.get_generated_sql(dialect)
        except UnSupportedError as error:
            raise UnSupportedError(
                f"{meta.full_name!r} can't be kept by the {dialect.name} database of connection "
                f"{meta.default_connection!r}: {error}"
            ) from error

    @staticmethod
    def raise_if_unique_constraint_options_unsupported(model: type[Model]) -> None:
        """Refuses a ``UniqueConstraint`` of ``nulls_distinct`` or ``without_overlaps`` on a database
        without the clause - before a migration of it is written, which no migrate could apply.

        Args:
            model: The model, bound to its connection.

        Raises:
            ConfigurationError: A constraint sets such an option.
        """
        meta = model._meta
        features = meta.connection.features
        for constraint in meta.constraints:
            if not isinstance(constraint, UniqueConstraint):
                continue
            for option, is_set, is_supported in (
                ("nulls_distinct", constraint.nulls_distinct is not None, features.supports_nulls_distinct),
                ("without_overlaps", bool(constraint.without_overlaps), features.supports_without_overlaps),
            ):
                if is_set and not is_supported:
                    constraint_name = constraint.name or ", ".join(constraint.fields)
                    raise ConfigurationError(
                        f"{meta.full_name!r} declares UniqueConstraint({constraint_name}) with {option}, which the "
                        f"{meta.connection.dialect.name} database of connection {meta.default_connection!r} doesn't "
                        "support"
                    )
