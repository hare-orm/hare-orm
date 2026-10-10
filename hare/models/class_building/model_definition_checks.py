from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from hare.ddl.conditions.tenant_condition import TenantCondition
from hare.ddl.constraints.unique_constraint import UniqueConstraint
from hare.ddl.indexes.index import Index
from hare.exceptions import ConfigurationError
from hare.fields.data.numeric.int_field import IntField
from hare.fields.data.temporal.datetime_field import DatetimeField
from hare.fields.relations.fields.foreign_key_field_instance import ForeignKeyFieldInstance
from hare.models.class_building.constants import UNSUPPORTED_META_OPTIONS
from hare.models.enums import ModelOption
from hare.sql.constants import IDENTIFIER_LENGTH_LIMIT

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.models.meta_info import MetaInfo
    from hare.models.model import Model


class ModelDefinitionChecks:
    """The checks of a model's definition made once its fields are known: identifier lengths, the soft
    delete, tenant and optimistic lock fields, the fields indexes and unique constraints name, and
    Meta options the model can't take."""

    @staticmethod
    def raise_if_unsupported_option_declared(meta: type[Model.Meta] | None) -> None:
        """Refuses a Meta option a model can no longer declare (``UNSUPPORTED_META_OPTIONS``).

        Args:
            meta: The model's ``Meta`` class.

        Raises:
            ConfigurationError: ``Meta`` declares such an option.
        """
        for option, replacement in UNSUPPORTED_META_OPTIONS.items():
            if getattr(meta, option, None):
                raise ConfigurationError(f"Meta.{option} isn't supported - {replacement}")

    @staticmethod
    def raise_if_relation_names_repeat(meta: MetaInfo) -> None:
        """Refuses a view, materialized view, dictionary and sequence of one name - one namespace
        holds them.

        Args:
            meta: The model's meta.

        Raises:
            ConfigurationError: Two of them share a name.
        """
        names: set[str] = set()
        for schema_object in (*meta.views, *meta.materialized_views, *meta.dictionaries, *meta.sequences):
            if schema_object.name in names:
                raise ConfigurationError(
                    "Meta declares a view, materialized view, dictionary or sequence named "
                    f"{schema_object.name!r} twice"
                )
            names.add(schema_object.name)

    @staticmethod
    def validate_identifier_lengths(meta: MetaInfo) -> None:
        """Rejects a table, column, index or constraint name Postgres would silently truncate -
        two such names can end up the same identifier, and the schema never matches the model.

        Args:
            meta: The model's meta.

        Raises:
            ConfigurationError: A name is longer than Postgres's identifier limit.
        """
        names: list[tuple[str, str]] = [("the table", meta.db_table)]
        names += [
            (f"the column of {field_name!r}", column) for field_name, column in meta.fields_db_projection.items()
        ]
        for field_name in meta.many_to_many_fields:
            many_to_many_field = meta.fields_map[field_name]
            through = getattr(many_to_many_field, "through", None)
            if isinstance(through, str):
                names.append((f"the through table of {field_name!r}", through))
            for key in (
                getattr(many_to_many_field, "forward_key", None),
                getattr(many_to_many_field, "backward_key", None),
            ):
                if isinstance(key, str):
                    names.append((f"the through table column of {field_name!r}", key))
        names += [("the index", index.name) for index in meta.indexes if isinstance(index, Index) and index.name]
        names += [
            ("the constraint", constraint_name)
            for constraint in meta.constraints
            if (constraint_name := getattr(constraint, "name", None))
        ]
        for trigger in meta.triggers:
            names += [("the trigger", trigger.name), ("the function of the trigger", trigger.function_name)]
        names += [("the view", view.name) for view in meta.views]
        names += [("the materialized view", view.name) for view in meta.materialized_views]
        names += [("the dictionary", dictionary.name) for dictionary in meta.dictionaries]
        names += [("the function", function.name) for function in meta.functions]
        names += [("the sequence", sequence.name) for sequence in meta.sequences]
        names += [("the policy", policy.name) for policy in meta.policies]
        for description, name in names:
            if len(name.encode()) > IDENTIFIER_LENGTH_LIMIT:
                raise ConfigurationError(
                    f"{meta._model.__name__}: {description} {name!r} is {len(name.encode())} bytes - longer than "
                    f"{IDENTIFIER_LENGTH_LIMIT} bytes, the identifier limit of a registered dialect, which would "
                    "truncate or reject it. Give it a shorter name."
                )

    @staticmethod
    def validate_distinct_columns(meta: MetaInfo) -> None:
        """Rejects two fields that map to the same database column.

        Args:
            meta: The model's meta.

        Raises:
            ConfigurationError: If two fields share a column.
        """
        field_name_by_column_name: dict[str, str] = {}
        for field_name, column_name in meta.fields_db_projection.items():
            reference = meta.fields_map[field_name].reference
            owner_field_name = reference.model_field_name if reference is not None else field_name
            other_field_name = field_name_by_column_name.setdefault(column_name, owner_field_name)
            if other_field_name != owner_field_name:
                raise ConfigurationError(
                    f"Fields '{other_field_name}' and '{owner_field_name}' on {meta._model.__name__} both map "
                    f"to the database column '{column_name}' - give one of them a different source_field"
                )

    @staticmethod
    def validate_soft_delete_field(meta: MetaInfo) -> None:
        if not isinstance(meta.soft_delete_hard_cascade, bool):
            raise ConfigurationError(f"Meta.soft_delete_hard_cascade on {meta._model.__name__} must be a bool")
        if meta.soft_delete_field is None:
            if meta.soft_delete_hard_cascade:
                raise ConfigurationError(
                    f"Meta.soft_delete_hard_cascade on {meta._model.__name__} needs Meta.soft_delete_field"
                )
            return
        field = meta.fields_map.get(meta.soft_delete_field)
        if field is None:
            raise ConfigurationError(
                f"Meta.soft_delete_field '{meta.soft_delete_field}' is not a field on {meta._model.__name__}"
            )
        if not isinstance(field, DatetimeField) or not field.null:
            raise ConfigurationError(
                f"Meta.soft_delete_field '{meta.soft_delete_field}' on {meta._model.__name__} must be a "
                "DatetimeField(null=True) - it also records when the row was deleted"
            )

    @staticmethod
    def validate_tenant_field(meta: MetaInfo) -> None:
        """Checks ``Meta.tenant_field`` and turns a forward FK/O2O relation name into its own
        key column (``company`` -> ``company_id``).

        Args:
            meta: The model's meta.

        Raises:
            ConfigurationError: If it names no field, a relation with a composite key, or a
                field without its own column.
        """
        if meta.tenant_field is None:
            return
        field = meta.fields_map.get(meta.tenant_field)
        if field is None:
            raise ConfigurationError(
                f"Meta.tenant_field '{meta.tenant_field}' is not a field on {meta._model.__name__}"
            )
        if meta.tenant_field in meta.foreign_key_fields or meta.tenant_field in meta.one_to_one_fields:
            relation_field = cast("ForeignKeyFieldInstance[Any]", field)
            if len(relation_field.source_fields) != 1:
                raise ConfigurationError(
                    f"Meta.tenant_field '{meta.tenant_field}' on {meta._model.__name__} is a relation with a "
                    f"composite key {relation_field.source_fields} - name a single column field instead"
                )
            meta.tenant_field = relation_field.source_fields[0]
            return
        if meta.tenant_field not in meta.fields_db_projection:
            raise ConfigurationError(
                f"Meta.tenant_field '{meta.tenant_field}' on {meta._model.__name__} has no column of its own - "
                "name a regular field or a forward ForeignKeyField/OneToOneField"
            )

    @staticmethod
    def validate_tenant_schema(meta: MetaInfo) -> None:
        """Checks ``Meta.tenant_schema``: a flag, not given with ``Meta.schema``, and no relation of a
        shared model reaches a model whose table is in each tenant's schema.

        Args:
            meta: The model's meta.

        Raises:
            ConfigurationError: It isn't a bool, it is given with ``Meta.schema``, or a shared model
                has a relation to a model with it.
        """
        if not isinstance(meta.tenant_schema, bool):
            raise ConfigurationError(
                f"Meta.tenant_schema on {meta._model.__name__} must be True or False, got {meta.tenant_schema!r}"
            )
        if meta.tenant_schema and meta.schema is not None:
            raise ConfigurationError(
                f"Meta.tenant_schema on {meta._model.__name__} can't be given with Meta.schema - the table "
                "lives in the schema of the active tenant"
            )
        if meta.tenant_schema:
            return
        for field_name in (*meta.foreign_key_fields, *meta.one_to_one_fields, *meta.many_to_many_fields):
            relation_field = cast("ForeignKeyFieldInstance[Any]", meta.fields_map[field_name])
            if getattr(relation_field, "_generated", False):
                continue
            # A relation to a swapped model has no target model.
            related_model = relation_field.related_model
            if related_model is not None and related_model._meta.tenant_schema:
                raise ConfigurationError(
                    f'"{meta._model.__name__}.{field_name}" relates a model of the shared schema to '
                    f'"{related_model.__name__}", whose table is in each tenant\'s schema - '
                    "declare the relation on the tenant's model instead"
                )

    @staticmethod
    def validate_policies_have_row_level_security(meta: MetaInfo) -> None:
        """Refuses policies on a table whose row level security is off - the database keeps them
        without applying any.

        Args:
            meta: The model's meta.

        Raises:
            ConfigurationError: ``Meta.policies`` is declared and ``Meta.row_level_security`` is None.
        """
        if meta.policies and meta.row_level_security is None:
            raise ConfigurationError(
                f"{meta._model.__name__}: Meta.policies take effect only with row level security on - set "
                "Meta.row_level_security to RowLevelSecurity.ENABLED, or RowLevelSecurity.FORCED to filter the "
                "table owner's rows too"
            )

    @staticmethod
    def validate_tenant_row_level_security(meta: MetaInfo) -> None:
        """Finds a ``TenantCondition`` policy, which needs ``Meta.tenant_field``, and sets
        ``checks_tenant_client``.

        Args:
            meta: The model's meta.

        Raises:
            ConfigurationError: A policy reads ``TenantCondition`` and the model has no
                ``Meta.tenant_field``.
        """
        meta.tenant_row_level_security = any(
            isinstance(policy.using, TenantCondition) or isinstance(policy.with_check, TenantCondition)
            for policy in meta.policies
        )
        if meta.tenant_row_level_security and meta.tenant_field is None:
            raise ConfigurationError(
                f"{meta._model.__name__}: a TenantCondition policy keeps the rows to the transaction's tenants "
                "by Meta.tenant_field - declare it"
            )
        meta.checks_tenant_client = meta.tenant_schema or meta.tenant_row_level_security

    @staticmethod
    def validate_optimistic_lock_field(meta: MetaInfo) -> None:
        if meta.optimistic_lock_field is None:
            return
        field = meta.fields_map.get(meta.optimistic_lock_field)
        if field is None:
            raise ConfigurationError(
                f"Meta.optimistic_lock_field '{meta.optimistic_lock_field}' is not a field on {meta._model.__name__}"
            )
        if not isinstance(field, IntField) or field.null:
            raise ConfigurationError(
                f"Meta.optimistic_lock_field '{meta.optimistic_lock_field}' on {meta._model.__name__} must be a "
                "non-nullable IntField"
            )
        if meta.optimistic_lock_field in meta.primary_key_attribute_names:
            # The optimistic lock field is bumped on every save(); a primary key column names the
            # row saved - one column can't be both.
            raise ConfigurationError(
                f"Meta.optimistic_lock_field '{meta.optimistic_lock_field}' on {meta._model.__name__} can't also be "
                "part of the primary key - it needs to be bumped in place on save(), which conflicts "
                "with a primary key column identifying which row is being saved"
            )
        if field.generated:
            raise ConfigurationError(
                f"Meta.optimistic_lock_field '{meta.optimistic_lock_field}' on {meta._model.__name__} can't be a "
                "DB-generated field - every write bumps it in place"
            )

    @staticmethod
    def get_index_expressions(meta: MetaInfo) -> None:
        for index in meta.indexes:
            if isinstance(index, Index):
                index.get_expressions(meta._model)

    @staticmethod
    def validate_together_field_indexability(meta: MetaInfo) -> None:
        ModelDefinitionChecks.validate_together_entries(meta, meta.indexes, ModelOption.INDEXES)
        ModelDefinitionChecks.validate_unique_constraint_fields(meta)

    @staticmethod
    def validate_together_entries(
        meta: MetaInfo, entries: tuple[tuple[str, ...] | Index, ...], meta_attribute_name: str
    ) -> None:
        for entry in entries:
            field_names = entry.fields if isinstance(entry, Index) else entry
            # Field.indexable describes a plain btree index - a non-btree access method (e.g. GIN
            # over a JSONField) is exactly how such a column gets indexed.
            is_btree = not (isinstance(entry, Index) and entry.INDEX_TYPE)
            for field_name in field_names:
                field = meta.fields_map.get(field_name)
                if field is None:
                    raise ConfigurationError(
                        f"Meta.{meta_attribute_name} field '{field_name}' is not a field on {meta._model.__name__}"
                    )
                if is_btree and not field.indexable:
                    raise ConfigurationError(
                        f"Meta.{meta_attribute_name} field '{field_name}' ({field.__class__.__name__}) on "
                        f"{meta._model.__name__} can't be indexed"
                    )

    @staticmethod
    def validate_unique_constraint_fields(meta: MetaInfo) -> None:
        for constraint in meta.constraints:
            if not isinstance(constraint, UniqueConstraint):
                continue
            for field_name in constraint.fields:
                field = meta.fields_map.get(field_name)
                if field is None:
                    raise ConfigurationError(
                        f"Meta.constraints: UniqueConstraint field '{field_name}' is not a field on "
                        f"{meta._model.__name__}"
                    )
                if field_name in meta.many_to_many_fields:
                    raise ConfigurationError(
                        f"Meta.constraints: UniqueConstraint field '{field_name}' on {meta._model.__name__} "
                        "is a ManyToManyField, which has no column on this table"
                    )
                if not field.indexable:
                    raise ConfigurationError(
                        f"Meta.constraints: UniqueConstraint field '{field_name}' ({field.__class__.__name__}) "
                        f"on {meta._model.__name__} can't be indexed"
                    )

    @staticmethod
    def validate_unique_indexes_do_not_collide_with_constraints(meta: MetaInfo) -> None:
        """Rejects an ``Index(unique=True)`` and a ``UniqueConstraint`` over the same set of fields -
        two physical objects enforcing one rule. The field order doesn't matter.

        Args:
            meta: The model's meta.
        """
        unique_constraint_field_sets = {
            frozenset(constraint.fields) for constraint in meta.constraints if isinstance(constraint, UniqueConstraint)
        }
        for index in meta.indexes:
            if not isinstance(index, Index) or not index.unique or not index.fields:
                continue
            if frozenset(index.fields) in unique_constraint_field_sets:
                raise ConfigurationError(
                    f"Model {meta._model.__name__}: Meta.indexes has a unique Index(fields={index.fields!r}) "
                    "that covers the exact same fields as a Meta.constraints UniqueConstraint - both "
                    "independently enforce the same uniqueness rule as two separate physical unique "
                    "indexes, doubling write overhead for no benefit. Remove the redundant declaration."
                )

    @staticmethod
    def validate_unnamed_unique_constraints_are_distinct(meta: MetaInfo) -> None:
        """Two unnamed ``UniqueConstraint``s on the same fields, in the same order, get the same
        generated name - the second fails schema creation with a raw "already exists" error.

        Args:
            meta: The model's meta.

        Raises:
            ConfigurationError: Two such constraints are declared.
        """
        unnamed_field_lists: set[tuple[str, ...]] = set()
        for constraint in meta.constraints:
            if not isinstance(constraint, UniqueConstraint) or constraint.name:
                continue
            field_names = tuple(constraint.fields)
            if field_names in unnamed_field_lists:
                raise ConfigurationError(
                    f"Model {meta._model.__name__}: Meta.constraints declares two unnamed "
                    f"UniqueConstraint(fields={field_names!r}) - they get the same generated name. Remove "
                    "the redundant one, or give one of them a name."
                )
            unnamed_field_lists.add(field_names)
