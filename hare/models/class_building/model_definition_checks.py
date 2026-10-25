from __future__ import annotations

import difflib
from typing import TYPE_CHECKING, Any, cast

from hare.ddl.conditions.constraint_condition import ConstraintCondition
from hare.ddl.conditions.tenant_condition import TenantCondition
from hare.ddl.constraints.unique_constraint import UniqueConstraint
from hare.ddl.enums import TriggerTiming
from hare.ddl.indexes.index import Index
from hare.dialects.base.literals.constants import SQL_NULL_BYTE
from hare.exceptions import ConfigurationError
from hare.fields.data.containers.container_field import ContainerField
from hare.fields.data.json.json_field import JSONField
from hare.fields.data.numeric.int_field import IntField
from hare.fields.data.temporal.datetime_field import DatetimeField
from hare.fields.relations.fields.foreign_key_field_instance import ForeignKeyFieldInstance
from hare.models.class_building.constants import (
    LOOKUP_SEPARATOR,
    META_OPTION_VALUE_TYPES,
    NAMING_META_OPTIONS,
    NONE_TAKING_META_OPTIONS,
    TABLE_NAME_SEPARATOR,
    UNSUPPORTED_META_OPTIONS,
)
from hare.models.class_building.extension_model_options import ExtensionModelOptions
from hare.models.enums import ModelOption
from hare.sql.constants import IDENTIFIER_LENGTH_LIMIT

if TYPE_CHECKING:
    from hare.models.meta_info import MetaInfo
    from hare.models.model import Model


class ModelDefinitionChecks:
    """The checks of a model's definition: its Meta options and their values when the class is built,
    the rest once its fields are known - names (identifier lengths, relation names, columns two fields
    share, declared names repeated), the soft delete, tenant, optimistic lock, primary key and
    ``get_latest_by`` fields, the schema options, the indexes and constraints and the fields they name,
    triggers, and row level security policies."""

    @staticmethod
    def raise_if_unsupported_option_declared(meta: type[Model.Meta] | None) -> None:
        """Refuses a Meta attribute that is no model option - of hare or of an extension
        (``ExtensionModelOptions``) - naming the option meant where one is near.

        Args:
            meta: The model's ``Meta`` class.

        Raises:
            ConfigurationError: ``Meta`` declares such an attribute.
        """
        if meta is None:
            return
        known_names = {option.value for option in ModelOption} | ExtensionModelOptions.names
        declared_names = {
            name
            for meta_class in meta.__mro__
            if meta_class is not object
            for name in vars(meta_class)
            if not name.startswith("__")
        }
        for name in sorted(declared_names - known_names):
            if name in UNSUPPORTED_META_OPTIONS:
                raise ConfigurationError(f"Meta.{name} isn't supported - {UNSUPPORTED_META_OPTIONS[name]}")
            near_names = difflib.get_close_matches(name, sorted(known_names), n=1)
            hint = f" - did you mean Meta.{near_names[0]}?" if near_names else ""
            raise ConfigurationError(f"Meta.{name} isn't a model option{hint}")

    @staticmethod
    def raise_if_option_values_mistyped(meta: type[Model.Meta] | None) -> None:
        """Refuses a Meta option the DDL writes given no text - it would fail far from the model, in the
        DDL.

        Args:
            meta: The model's ``Meta`` class.

        Raises:
            ConfigurationError: An option holds such a value, an empty text where it names
                something, an extension's name is no text, or a table name holds a dot.
        """
        if meta is None:
            return
        for name, (value_types, description) in META_OPTION_VALUE_TYPES.items():
            if not hasattr(meta, name):
                continue
            value = getattr(meta, name)
            if value is None and name in NONE_TAKING_META_OPTIONS:
                continue
            if not isinstance(value, value_types):
                raise ConfigurationError(f"Meta.{name} takes {description}, got {value!r}")
            if value == "" and name in NAMING_META_OPTIONS:
                raise ConfigurationError(f"Meta.{name} is empty - it names nothing")
        table = getattr(meta, ModelOption.TABLE, None)
        if isinstance(table, str) and TABLE_NAME_SEPARATOR in table:
            # The name of an automatic through table, made of it, would read as an "app.Model"
            # reference.
            raise ConfigurationError(f"Meta.table {table!r} holds a dot - a table's schema is named by Meta.schema")
        ModelDefinitionChecks.raise_if_option_entries_mistyped(meta)

    @staticmethod
    def raise_if_option_entries_mistyped(meta: type[Model.Meta]) -> None:
        """Refuses an extension's name that is no text - it is written into the DDL.

        Args:
            meta: The model's ``Meta`` class.

        Raises:
            ConfigurationError: An entry of ``extensions`` is no text, or an empty one.
        """
        for entry in getattr(meta, ModelOption.EXTENSIONS, ()):
            if not isinstance(entry, str) or entry == "":
                raise ConfigurationError(f"Meta.extensions entries are each the name of an extension, got {entry!r}")

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
    def validate_identifiers(meta: MetaInfo) -> None:
        """Rejects a table, column, index or constraint name the schema can't hold: an empty one, one
        with a null byte, and one a database would silently truncate - two such names can end up the
        same identifier, and the schema never matches the model.

        Args:
            meta: The model's meta.

        Raises:
            ConfigurationError: A name is empty, holds a null byte or is longer than the identifier
                limit.
        """
        names: list[tuple[str, str]] = [("the table", meta.db_table)]
        names += [
            (f"the column of {field_name!r}", column) for field_name, column in meta.fields_db_projection.items()
        ]
        for field_name in meta.many_to_many_fields:
            many_to_many_field = meta.fields_map[field_name]
            through = getattr(many_to_many_field, "through", None)
            if isinstance(through, str) and through:
                names.append((f"the through table of {field_name!r}", through))
            for key in (
                getattr(many_to_many_field, "forward_key", None),
                getattr(many_to_many_field, "backward_key", None),
            ):
                if isinstance(key, str) and key:
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
            if not name or SQL_NULL_BYTE in name:
                raise ConfigurationError(
                    f"{meta._model.__name__}: {description} {name!r} isn't a name the schema can hold - it is "
                    "empty or holds a null byte"
                )
            if len(name.encode()) > IDENTIFIER_LENGTH_LIMIT:
                raise ConfigurationError(
                    f"{meta._model.__name__}: {description} {name!r} is {len(name.encode())} bytes - longer than "
                    f"{IDENTIFIER_LENGTH_LIMIT} bytes, the identifier limit of a registered dialect, which would "
                    "truncate or reject it. Give it a shorter name."
                )

    @staticmethod
    def validate_triggers_fire_on_a_table(meta: MetaInfo) -> None:
        """Rejects an ``INSTEAD OF`` trigger of a model whose table migrations create - the
        database fires one on a view only.

        Args:
            meta: The model's meta.

        Raises:
            ConfigurationError: Such a trigger is declared.
        """
        if meta.managed is False:
            return
        for trigger in meta.triggers:
            if trigger.timing == TriggerTiming.INSTEAD_OF:
                raise ConfigurationError(
                    f"{meta._model.__name__}: Trigger {trigger.name!r} fires INSTEAD OF - the database fires such a "
                    "trigger on a view only, and a model's table takes BEFORE or AFTER"
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
                    f"Fields {other_field_name!r} and {owner_field_name!r} on {meta._model.__name__} both map "
                    f"to the database column {column_name!r} - give one of them a different source_field"
                )

    @staticmethod
    def validate_soft_delete_field(meta: MetaInfo) -> None:
        """Checks ``Meta.soft_delete_field``: a ``DatetimeField(null=True)`` of the model, and given when
        ``Meta.soft_delete_hard_cascade`` is.

        Args:
            meta: The model's meta.

        Raises:
            ConfigurationError: The option names no such field, or is missing for the hard cascade.
        """
        if meta.soft_delete_field is None:
            if meta.soft_delete_hard_cascade:
                raise ConfigurationError(
                    f"Meta.soft_delete_hard_cascade on {meta._model.__name__} needs Meta.soft_delete_field"
                )
            return
        field = meta.fields_map.get(meta.soft_delete_field)
        if field is None:
            raise ConfigurationError(
                f"Meta.soft_delete_field {meta.soft_delete_field!r} is not a field on {meta._model.__name__}"
            )
        if not isinstance(field, DatetimeField) or not field.null:
            raise ConfigurationError(
                f"Meta.soft_delete_field {meta.soft_delete_field!r} on {meta._model.__name__} must be a "
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
                f"Meta.tenant_field {meta.tenant_field!r} is not a field on {meta._model.__name__}"
            )
        if meta.tenant_field in meta.foreign_key_fields or meta.tenant_field in meta.one_to_one_fields:
            relation_field = cast("ForeignKeyFieldInstance[Any]", field)
            if len(relation_field.source_fields) != 1:
                raise ConfigurationError(
                    f"Meta.tenant_field {meta.tenant_field!r} on {meta._model.__name__} is a relation with a "
                    f"composite key {relation_field.source_fields} - name a single column field instead"
                )
            meta.tenant_field = relation_field.source_fields[0]
            return
        if meta.tenant_field not in meta.fields_db_projection:
            raise ConfigurationError(
                f"Meta.tenant_field {meta.tenant_field!r} on {meta._model.__name__} has no column of its own - "
                "name a regular field or a forward ForeignKeyField/OneToOneField"
            )

    @staticmethod
    def validate_schema(meta: MetaInfo) -> None:
        """Checks ``Meta.schema``: None for the connection's own schema, else the name of one.

        Args:
            meta: The model's meta.

        Raises:
            ConfigurationError: It is an empty or blank name.
        """
        if meta.schema is not None and not meta.schema.strip():
            raise ConfigurationError(
                f"Meta.schema on {meta._model.__name__} must be None or a schema name, got {meta.schema!r}"
            )

    @staticmethod
    def validate_tenant_schema(meta: MetaInfo) -> None:
        """Checks ``Meta.tenant_schema``: not given with ``Meta.schema``, and no relation of a shared
        model reaches a model whose table is in each tenant's schema.

        Args:
            meta: The model's meta.

        Raises:
            ConfigurationError: It is given with ``Meta.schema``, or a shared model has a relation to
                a model with it.
        """
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
                    f"{related_model.__name__!r}, whose table is in each tenant's schema - "
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
    def validate_field_names(meta: MetaInfo) -> None:
        """Refuses a field or reverse relation name holding the lookup separator - a filter key would
        split it into a path.

        Args:
            meta: The model's meta.

        Raises:
            ConfigurationError: A name holds ``__``, or ends in ``_`` - ``from___gt`` would read as the
                field ``from`` and the lookup ``_gt``.
        """
        for field_name in meta.fields_map:
            if field_name.endswith("_"):
                raise ConfigurationError(
                    f"{meta._model.__name__}.{field_name}: a field name can't end in '_' - a lookup path "
                    f"({field_name}_{LOOKUP_SEPARATOR}gt) would read its last '_' as the separator's; name it "
                    f"otherwise and keep the column with source_field={field_name.rstrip('_')!r}"
                )
        for field_name in meta.fields_map:
            if LOOKUP_SEPARATOR in field_name:
                raise ConfigurationError(
                    f"{meta._model.__name__}.{field_name}: a field name can't contain {LOOKUP_SEPARATOR!r} - "
                    "it separates the parts of a lookup path"
                )

    @staticmethod
    def validate_primary_key_values(meta: MetaInfo) -> None:
        """Refuses a primary key holding a container value - an array, a map, a tuple, nested rows or a
        JSON document: the ORM keeps the rows it reads, prefetches and writes by their key, which needs a
        value equal to itself in a set, and compares keys with ``__in``, which a container takes none of.

        Args:
            meta: The model's meta.

        Raises:
            ConfigurationError: A field of the primary key holds a container value.
        """
        for name in meta.primary_key_attribute_names:
            field = meta.fields_map.get(name)
            if isinstance(field, (ContainerField, JSONField)):
                raise ConfigurationError(
                    f"{meta._model.__name__}.{name} ({type(field).__name__}) can't be part of the primary key - "
                    "the ORM keeps rows by their key, which holds no array, map, tuple or JSON document; key "
                    "the model by other fields and make these unique with a UniqueConstraint"
                )

    @staticmethod
    def validate_latest_by_fields(meta: MetaInfo) -> None:
        """Refuses a ``Meta.get_latest_by`` name that is not a field of the model.

        Args:
            meta: The model's meta.

        Raises:
            ConfigurationError: A name is unknown.
        """
        unknown_names = [
            name
            for name in meta.get_latest_by
            if name.removeprefix("-").partition(LOOKUP_SEPARATOR)[0] not in {*meta.fields_map, "pk"}
        ]
        if unknown_names:
            raise ConfigurationError(
                f"Meta.get_latest_by of {meta._model.__name__} names unknown fields {', '.join(unknown_names)}"
            )

    @staticmethod
    def validate_optimistic_lock_field(meta: MetaInfo) -> None:
        """Checks ``Meta.optimistic_lock_field``: a non-nullable ``IntField`` of the model, outside its
        primary key and not generated by the database - every save bumps it in place.

        Args:
            meta: The model's meta.

        Raises:
            ConfigurationError: The option names no such field.
        """
        if meta.optimistic_lock_field is None:
            return
        field = meta.fields_map.get(meta.optimistic_lock_field)
        if field is None:
            raise ConfigurationError(
                f"Meta.optimistic_lock_field {meta.optimistic_lock_field!r} is not a field on {meta._model.__name__}"
            )
        if not isinstance(field, IntField) or field.null:
            raise ConfigurationError(
                f"Meta.optimistic_lock_field {meta.optimistic_lock_field!r} on {meta._model.__name__} must be a "
                "non-nullable IntField"
            )
        if meta.optimistic_lock_field in meta.primary_key_attribute_names:
            # The optimistic lock field is bumped on every save(); a primary key column names the
            # row saved - one column can't be both.
            raise ConfigurationError(
                f"Meta.optimistic_lock_field {meta.optimistic_lock_field!r} on {meta._model.__name__} can't also be "
                "part of the primary key - it needs to be bumped in place on save(), which conflicts "
                "with a primary key column identifying which row is being saved"
            )
        if field.generated:
            raise ConfigurationError(
                f"Meta.optimistic_lock_field {meta.optimistic_lock_field!r} on {meta._model.__name__} can't be a "
                "DB-generated field - every write bumps it in place"
            )

    @staticmethod
    def check_index_expressions(meta: MetaInfo) -> None:
        """Builds the expressions of every ``Index`` of ``Meta.indexes``, so that one naming what the model
        lacks is refused when the model is defined, not when its schema is written.

        Args:
            meta: The model's meta.

        Raises:
            FieldError: An expression names what the model lacks.
        """
        for index in meta.indexes:
            if isinstance(index, Index):
                index.get_expressions(meta._model)

    @staticmethod
    def validate_unnamed_indexes_are_distinct(meta: MetaInfo) -> None:
        """Two unnamed indexes over the same keys, of the same type and options, get the same
        generated name - the second fails schema creation with a raw "already exists" error.

        Args:
            meta: The model's meta, its index expressions resolved.

        Raises:
            ConfigurationError: Two such indexes are declared.
        """
        name_sources: set[tuple[Any, ...]] = set()
        for entry in meta.indexes:
            index = entry if isinstance(entry, Index) else Index(fields=tuple(entry))
            if index.name:
                continue
            name_source = (index.unique, tuple(index.field_names), index.get_name_parts())
            if name_source in name_sources:
                raise ConfigurationError(
                    f"Model {meta._model.__name__}: Meta.indexes declares the same unnamed index over "
                    f"{list(index.field_names)!r} twice - they get the same generated name. Remove the "
                    "redundant one, or give one of them a name."
                )
            name_sources.add(name_source)

    @staticmethod
    def validate_index_fields(meta: MetaInfo) -> None:
        """Checks the fields ``Meta.indexes`` and the unique constraints of ``Meta.constraints`` name.

        Args:
            meta: The model's meta.

        Raises:
            ConfigurationError: A name is of no field of the model, or of a field that can't be indexed.
        """
        ModelDefinitionChecks.validate_index_entries(meta)
        ModelDefinitionChecks.validate_unique_constraint_fields(meta)

    @staticmethod
    def validate_index_entries(meta: MetaInfo) -> None:
        """Checks every entry of ``Meta.indexes`` - a list of field names or an ``Index``: each field
        is one of the model, with a column of its table (not a ``ManyToManyField``), indexable by a plain
        index unless the index has an access method of its own, and each ``include`` name a stored field.

        Args:
            meta: The model's meta.

        Raises:
            ConfigurationError: An entry names a field it can't index.
        """
        meta_attribute_name = ModelOption.INDEXES.value
        for entry in meta.indexes:
            field_names = entry.fields if isinstance(entry, Index) else entry
            # Field.indexable describes a plain btree index - a non-btree access method (e.g. GIN
            # over a JSONField) is exactly how such a column gets indexed.
            is_btree = not (isinstance(entry, Index) and entry.INDEX_TYPE)
            for field_name in field_names:
                field = meta.fields_map.get(field_name)
                if field is None:
                    raise ConfigurationError(
                        f"Meta.{meta_attribute_name} field {field_name!r} is not a field on {meta._model.__name__}"
                    )
                if field_name in meta.many_to_many_fields:
                    raise ConfigurationError(
                        f"Meta.{meta_attribute_name} field {field_name!r} on {meta._model.__name__} is a "
                        "ManyToManyField, which has no column on this table"
                    )
                if is_btree and not field.indexable:
                    raise ConfigurationError(
                        f"Meta.{meta_attribute_name} field {field_name!r} ({field.__class__.__name__}) on "
                        f"{meta._model.__name__} can't be indexed"
                    )
            for field_name in entry.include if isinstance(entry, Index) else ():
                if not ModelDefinitionChecks.is_stored_field(meta, field_name):
                    raise ConfigurationError(
                        f"Meta.{meta_attribute_name} include {field_name!r} is not a field stored on "
                        f"{meta._model.__name__}"
                    )

    @staticmethod
    def is_stored_field(meta: MetaInfo, field_name: str) -> bool:
        """Whether a name an index's or a constraint's ``include`` gives stands for columns of the table - a
        field with a column, or a forward relation standing for its key columns.

        Args:
            meta: The model's meta.
            field_name: The name.

        Returns:
            Whether it does.
        """
        return (
            field_name in meta.fields_db_projection
            or field_name in meta.foreign_key_fields
            or field_name in meta.one_to_one_fields
        )

    @staticmethod
    def validate_unique_constraint_fields(meta: MetaInfo) -> None:
        """Checks the fields of every ``UniqueConstraint`` of ``Meta.constraints``: each is an indexable
        field with a column of the table, each ``include`` name a stored field, and the condition reads
        only the model's columns.

        Args:
            meta: The model's meta.

        Raises:
            ConfigurationError: A constraint names a field it can't hold, or its condition reads other
                columns.
        """
        for constraint in meta.constraints:
            if not isinstance(constraint, UniqueConstraint):
                continue
            for field_name in constraint.fields:
                field = meta.fields_map.get(field_name)
                if field is None:
                    raise ConfigurationError(
                        f"Meta.constraints: UniqueConstraint field {field_name!r} is not a field on "
                        f"{meta._model.__name__}"
                    )
                if field_name in meta.many_to_many_fields:
                    raise ConfigurationError(
                        f"Meta.constraints: UniqueConstraint field {field_name!r} on {meta._model.__name__} "
                        "is a ManyToManyField, which has no column on this table"
                    )
                if not field.indexable:
                    raise ConfigurationError(
                        f"Meta.constraints: UniqueConstraint field {field_name!r} ({field.__class__.__name__}) "
                        f"on {meta._model.__name__} can't be indexed"
                    )
            for field_name in constraint.include:
                if not ModelDefinitionChecks.is_stored_field(meta, field_name):
                    raise ConfigurationError(
                        f"Meta.constraints: UniqueConstraint include {field_name!r} is not a field stored on "
                        f"{meta._model.__name__}"
                    )
            # Refused when the model is defined, alike on every database - not only by the DDL of a database
            # writing the condition.
            ConstraintCondition.raise_if_reads_other_columns(constraint.condition, meta._model)

    @staticmethod
    def validate_declared_names_are_distinct(meta: MetaInfo) -> None:
        """Refuses two constraints or indexes of a model declared under one name - the database
        refuses the second when the schema is made, or keeps both.

        Args:
            meta: The model's meta.

        Raises:
            ConfigurationError: Two of them share a name.
        """
        names: set[str] = set()
        for declaration in (*meta.constraints, *meta.indexes):
            name = getattr(declaration, "name", None)
            if not name:
                continue
            if name in names:
                raise ConfigurationError(
                    f"Model {meta._model.__name__}: Meta.constraints and Meta.indexes declare two objects named "
                    f"{name!r}"
                )
            names.add(name)

    @staticmethod
    def validate_unique_indexes_do_not_collide_with_constraints(meta: MetaInfo) -> None:
        """Rejects an ``Index(unique=True)`` and a ``UniqueConstraint`` over the same set of fields and
        the same condition - two physical objects enforcing one rule. The field order doesn't matter;
        a different condition is a different rule.

        Args:
            meta: The model's meta.
        """
        unique_constraint_rules = [
            (frozenset(constraint.fields), constraint.condition)
            for constraint in meta.constraints
            if isinstance(constraint, UniqueConstraint)
        ]
        for index in meta.indexes:
            if not isinstance(index, Index) or not index.unique or not index.fields:
                continue
            if (frozenset(index.fields), getattr(index, "condition", None)) in unique_constraint_rules:
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
