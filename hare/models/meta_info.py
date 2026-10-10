from __future__ import annotations

import inspect
from collections.abc import Callable, Iterable
from typing import TYPE_CHECKING, Any, cast

from hare.core.caching.caches import Caches
from hare.core.connections.connections import Connections
from hare.core.constants import SWAPPABLE_SETTING_NAME_PATTERN
from hare.ddl.constraints.unique_constraint import UniqueConstraint
from hare.ddl.enums import RowLevelSecurity
from hare.ddl.indexes.index import Index
from hare.ddl.schema_objects.database_function import DatabaseFunction
from hare.ddl.schema_objects.database_sequence import DatabaseSequence
from hare.ddl.schema_objects.dictionary import Dictionary
from hare.ddl.schema_objects.materialized_view import MaterializedView
from hare.ddl.schema_objects.view import View
from hare.ddl.security.grant import Grant
from hare.ddl.security.policy import Policy
from hare.ddl.table_options import TableOptions
from hare.dialects.base.client.database_client import DatabaseClient
from hare.exceptions import ConfigurationError, HareError, QueryError
from hare.fields.constants import FOREIGN_KEY_COLUMN_SUFFIX
from hare.fields.field import Field
from hare.fields.relations.fields.backward_foreign_key_relation import BackwardForeignKeyRelation
from hare.fields.relations.fields.declarations import BackwardOneToOneRelation
from hare.fields.relations.fields.foreign_key_field_instance import ForeignKeyFieldInstance
from hare.fields.relations.fields.generic_foreign_key_field_instance import GenericForeignKeyFieldInstance
from hare.fields.relations.fields.many_to_many_field_instance import ManyToManyFieldInstance
from hare.fields.relations.fields.one_to_one_field_instance import OneToOneFieldInstance
from hare.instrumentation.capture.change_capturing import ChangeCapturing
from hare.models.class_building.lazy_relation_fields import LazyRelationFields
from hare.models.class_building.model_definition_checks import ModelDefinitionChecks
from hare.models.class_building.schema_object_declarations import SchemaObjectDeclarations
from hare.models.enums import ModelOption
from hare.models.tenancy.tenant_row_level_security import TenantRowLevelSecurity
from hare.query.lookup_info.lookup_info import LookupInfo
from hare.query.lookup_info.lookup_info_builder import LookupInfoBuilder
from hare.query.lookup_info.ordering_info import OrderingInfo
from hare.query.managers.manager import Manager
from hare.query.queryset import QuerySet
from hare.query.rows.native.hydration_layout import HydrationLayout
from hare.sql import Order, Query, Table
from hare.sql.builder.queries.query_builder import QueryBuilder

if TYPE_CHECKING:
    from hare.ddl.constraints.check_constraint import CheckConstraint
    from hare.ddl.constraints.exclusion_constraint import ExclusionConstraint
    from hare.ddl.constraints.foreign_key_constraint import ForeignKeyConstraint
    from hare.ddl.schema_objects.trigger import Trigger
    from hare.dialects.base.dialect import Dialect
    from hare.instrumentation.capture.capture_needs import CaptureNeeds
    from hare.instrumentation.capture.change_sink import ChangeSink
    from hare.models.model import Model

    ModelConstraint = UniqueConstraint | CheckConstraint | ExclusionConstraint | ForeignKeyConstraint


class MetaInfo:
    __slots__ = (
        "_default_ordering",
        "_foreign_key_or_one_to_one_inited",
        "_inited",
        "_model",
        "_ordering_validated",
        "abstract",
        "app",
        "backward_foreign_key_fields",
        "backward_one_to_one_fields",
        "generic_foreign_key_fields",
        "registered_live",
        "basequery",
        "basequery_all_fields",
        "basetable",
        "constraints",
        "db_default_db_columns",
        "db_fields",
        "db_pk_column",
        "db_table",
        "default_connection",
        "default_custom_generated_pk",
        "direct_fields",
        "extensions",
        "fetch_fields",
        "fields",
        "fields_db_projection",
        "fields_db_projection_reverse",
        "fields_map",
        "foreign_key_fields",
        "foreign_key_shadow_columns",
        "generated_db_fields",
        "generated_db_table",
        "generated_pk_field_name",
        "get_latest_by",
        "setattr_hooks",
        "next_setattr",
        "instance_constructor",
        "hidden_backward_relations",
        "indexes",
        "many_to_many_fields",
        "managed",
        "manager",
        "meta",
        "one_to_one_fields",
        "own_field_names",
        "pk",
        "primary_key_attribute",
        "pk_without_overlaps",
        "pk_fields",
        "recomputed_db_fields",
        "returning",
        "schema",
        "sensitive_fields",
        "blind_index_fields",
        "plan_cache_buckets",
        "lookup_paths",
        "concrete_field_paths",
        "lookup_infos",
        "ordering_infos",
        "lookups_by_path",
        "hydration_layouts",
        "model_cache_values",
        "soft_delete_field",
        "soft_delete_hard_cascade",
        "swappable",
        "swapped",
        "table_description",
        "table_is_explicit",
        "table_options",
        "tenant_field",
        "tenant_schema",
        "tenant_row_level_security",
        "checks_tenant_client",
        "track_dirty_fields",
        "change_capture",
        "change_capture_needs",
        "triggers",
        "optimistic_lock_field",
        "views",
        "materialized_views",
        "dictionaries",
        "functions",
        "sequences",
        "policies",
        "grants",
        "row_level_security",
    )

    def __init__(self, meta: type[Model.Meta] | None) -> None:
        """
        Args:
            meta: The model's Meta, None for the base model.
        """
        self._read_table_names(meta)
        self._read_schema_objects(meta)
        self._read_query_options(meta)
        self._init_fields(meta)
        self._read_row_options(meta)
        self._init_derived_state(meta)

    def _read_table_names(self, meta: type[Model.Meta] | None) -> None:
        """The options naming the model's table, and its manager.

        Args:
            meta: The model's Meta.
        """
        #: The model's Meta, the Meta of its abstract ancestors merged in - an extension reads its own
        #: options off it (an outbox model's ``Meta.outbox_wakeup``).
        self.meta = meta
        self.abstract: bool = getattr(meta, ModelOption.ABSTRACT, False)
        # A fresh manager, never the Meta attribute's own object: an abstract base's Meta.manager is
        # one object for every subclass, and each model binds its manager to itself. Read without
        # running the descriptor - off a class it gives a queryset.
        declared_manager: Manager | None = inspect.getattr_static(meta, ModelOption.MANAGER, None)
        self.manager: Manager = declared_manager.copy_unbound() if declared_manager is not None else Manager()
        self.db_table: str = getattr(meta, ModelOption.TABLE, "")
        # Whether Meta.table was declared rather than left to be derived from the class name - a
        # RenameModel tells the two apart. `meta.table_is_explicit` overrides it: a Meta rebuilt
        # from a migration always carries a table.
        explicit_override = getattr(meta, ModelOption.TABLE_IS_EXPLICIT, None)
        self.table_is_explicit: bool = explicit_override if explicit_override is not None else bool(self.db_table)
        #: The table name Apps last generated for this model (table_name_generator or the class
        #: name) - None while db_table came from Meta.table. Lets every init regenerate it instead
        #: of keeping the first init's name.
        self.generated_db_table: str | None = None
        self.schema: str | None = getattr(meta, ModelOption.SCHEMA, None)
        self.app: str | None = getattr(meta, ModelOption.APP, None)
        ModelDefinitionChecks.raise_if_unsupported_option_declared(meta)

    def _read_schema_objects(self, meta: type[Model.Meta] | None) -> None:
        """The schema objects the model declares besides its table.

        Args:
            meta: The model's Meta.
        """
        self.constraints: tuple[ModelConstraint, ...] = tuple(getattr(meta, ModelOption.CONSTRAINTS, ()))
        self.triggers: tuple[Trigger, ...] = tuple(getattr(meta, ModelOption.TRIGGERS, ()))
        self.views: tuple[View, ...] = SchemaObjectDeclarations.get_schema_objects(
            meta, ModelOption.VIEWS, View, MaterializedView
        )
        self.materialized_views: tuple[MaterializedView, ...] = SchemaObjectDeclarations.get_schema_objects(
            meta, ModelOption.MATERIALIZED_VIEWS, MaterializedView
        )
        self.dictionaries: tuple[Dictionary, ...] = SchemaObjectDeclarations.get_schema_objects(
            meta, ModelOption.DICTIONARIES, Dictionary
        )
        self.functions: tuple[DatabaseFunction, ...] = SchemaObjectDeclarations.get_schema_objects(
            meta, ModelOption.FUNCTIONS, DatabaseFunction
        )
        self.sequences: tuple[DatabaseSequence, ...] = SchemaObjectDeclarations.get_schema_objects(
            meta, ModelOption.SEQUENCES, DatabaseSequence
        )
        self.policies: tuple[Policy, ...] = SchemaObjectDeclarations.get_schema_objects(
            meta, ModelOption.POLICIES, Policy
        )
        self.grants: tuple[Grant, ...] = SchemaObjectDeclarations.get_schema_objects(meta, ModelOption.GRANTS, Grant)
        ModelDefinitionChecks.raise_if_relation_names_repeat(self)
        self.row_level_security: RowLevelSecurity | None = SchemaObjectDeclarations.get_row_level_security(meta)
        self.indexes: tuple[tuple[str, ...] | Index, ...] = self._get_together(meta, ModelOption.INDEXES)
        self.extensions: tuple[str, ...] = tuple(getattr(meta, ModelOption.EXTENSIONS, ()))

    def _read_query_options(self, meta: type[Model.Meta] | None) -> None:
        """The options of the model's swapping and of its queries' ordering.

        Args:
            meta: The model's Meta.
        """
        #: The ``swappable`` config setting a project may point at another model instead of this one.
        self.swappable: str | None = getattr(meta, ModelOption.SWAPPABLE, None)
        if self.swappable is not None and (
            not isinstance(self.swappable, str) or not SWAPPABLE_SETTING_NAME_PATTERN.fullmatch(self.swappable)
        ):
            raise ConfigurationError(
                f'Meta.swappable must be an upper-case setting name such as "USER_MODEL", got {self.swappable!r}'
            )
        #: The ``app_label.ModelName`` label of the model the ``swappable`` setting points at
        #: instead of this one - None while this model is the one in use.
        self.swapped: str | None = None
        self._default_ordering: tuple[tuple[str, Order], ...] = self._prepare_default_ordering(meta)
        self._ordering_validated: bool = False
        #: The fields ``latest()``/``earliest()`` order by when given none - ``Meta.get_latest_by``.
        self.get_latest_by: tuple[str, ...] = self._get_latest_by_fields(meta)

    def _init_fields(self, meta: type[Model.Meta] | None) -> None:
        """The registries of the model's fields and its primary key - filled when the class is built.

        Args:
            meta: The model's Meta.
        """
        self.fields: set[str] = set()
        #: Every column of the table, in the order the fields are declared - the SELECT column list.
        self.db_fields: tuple[str, ...] = ()
        self.many_to_many_fields: set[str] = set()
        self.foreign_key_fields: set[str] = set()
        #: Key column field name ("author_id") -> the attribute caching its relation's related
        #: object ("_author") - assigning the column drops that cache.
        self.foreign_key_shadow_columns: dict[str, str] = {}
        self.one_to_one_fields: set[str] = set()
        self.backward_foreign_key_fields: set[str] = set()
        self.backward_one_to_one_fields: set[str] = set()
        #: The generic foreign keys by name - each is no field of its own, its branches are.
        self.generic_foreign_key_fields: dict[str, GenericForeignKeyFieldInstance[Any]] = {}
        #: Whether ``register_live_models()`` registered the model.
        self.registered_live = False
        #: Backward FK/O2O relations of forward fields declared with ``related_name=False``, keyed
        #: by the forward field's ``"app.Model.field"`` - no accessor, filter or fetch field, only
        #: seen by the ``on_delete`` cascade.
        self.hidden_backward_relations: dict[str, BackwardForeignKeyRelation[Any]] = {}
        self.fetch_fields: set[str] = set()
        self.direct_fields: frozenset[str] = frozenset()
        #: Names of every field declared (or, for an FK/O2O shadow column, inherited from its
        #: relation) with ``sensitive=True``.
        self.sensitive_fields: frozenset[str] = frozenset()
        #: The blind index field of each encrypted field declared with ``blind_index=True``.
        self.blind_index_fields: dict[str, str] = {}
        self.fields_db_projection: dict[str, str] = {}
        self.fields_db_projection_reverse: dict[str, str] = {}
        self.fields_map: dict[str, Field[Any]] = {}
        #: The fields this class's own body declares, a synthesized `id` included - read by a
        #: subclass resolving a diamond.
        self.own_field_names: frozenset[str] = frozenset()
        self._inited: bool = False
        self._foreign_key_or_one_to_one_inited: bool = False
        self.default_connection: str | None = None
        self.basequery: QueryBuilder = QueryBuilder()
        self.basequery_all_fields: QueryBuilder = QueryBuilder()
        self.basetable: Table = Table("")
        self.primary_key_attribute: str | tuple[str, ...] = getattr(meta, ModelOption.PRIMARY_KEY_ATTRIBUTE, "")
        #: Only set when primary_key_attribute is a tuple (composite PK) - the actual Field objects, in the
        #: same order as primary_key_attribute. Empty for a single-column PK (use .pk instead).
        self.pk_fields: tuple[Field[Any], ...] = ()
        #: Whether a composite primary key compares its last field WITHOUT OVERLAPS.
        self.pk_without_overlaps: bool = False

    def _read_row_options(self, meta: type[Model.Meta] | None) -> None:
        """The options about the way the model's rows are written - soft delete, tenants, locking,
        change capture.

        Args:
            meta: The model's Meta.
        """
        self.soft_delete_field: str | None = getattr(meta, ModelOption.SOFT_DELETE_FIELD, None)
        # Whether a soft delete of the model's rows really deletes the cascaded rows that can't be
        # soft-deleted themselves and the M2M through rows, instead of keeping them for restore().
        self.soft_delete_hard_cascade: bool = getattr(meta, ModelOption.SOFT_DELETE_HARD_CASCADE, False)
        # The column holding the tenant of a multi-tenant model - of any field type.
        self.tenant_field: str | None = getattr(meta, ModelOption.TENANT_FIELD, None)
        #: Whether the table lives in each tenant's own schema (the connection's
        #: ``tenant_schema_template``) rather than once in the shared one.
        self.tenant_schema: bool = getattr(meta, ModelOption.TENANT_SCHEMA, False)
        #: Whether a ``Meta.policies`` policy reads ``TenantCondition`` - the database keeps the rows
        #: to the transaction's tenants.
        self.tenant_row_level_security: bool = False
        #: Whether a query checks its client with ``check_tenant_client()``.
        self.checks_tenant_client: bool = False
        self.optimistic_lock_field: str | None = getattr(meta, ModelOption.OPTIMISTIC_LOCK_FIELD, None)
        # Opt-in: an unconditional per-row snapshot would cost every hydration, not just the
        # models that actually use .get_dirty_fields() - see ModelColumns.compile_hydrate_function().
        self.track_dirty_fields: bool = getattr(meta, ModelOption.TRACK_DIRTY_FIELDS, False)
        #: Where the rows of the model's writes go, in the write's transaction (``Meta.change_capture``).
        self.change_capture: ChangeSink | None = getattr(meta, ModelOption.CHANGE_CAPTURE, None)
        #: What the writes read for ``change_capture`` - worked out when the model is finalised; None
        #: for a model without it.
        self.change_capture_needs: CaptureNeeds | None = None
        # False: the table is never hare's to create, alter or drop - migrations and drift leave it
        # alone.
        self.managed: bool = getattr(meta, ModelOption.MANAGED, True)
        # The default of bulk_create()/bulk_update()'s `returning` when a call doesn't pass it.
        self.returning: bool = getattr(meta, ModelOption.RETURNING, False)

    def _init_derived_state(self, meta: type[Model.Meta] | None) -> None:
        """What is worked out when the model is set up, and the caches of the model.

        Args:
            meta: The model's Meta.
        """
        self.generated_db_fields: tuple[str, ...] = None  # type: ignore[assignment]
        #: The generated columns an UPDATE makes the database compute again - every one but the
        #: primary key, which an UPDATE never generates anew.
        self.recomputed_db_fields: tuple[str, ...] = None  # type: ignore[assignment]
        #: Name of the single-column primary key when the database generates it (autoincrement),
        #: else None - the only pk whose explicit assignment flips Model._custom_generated_pk.
        self.generated_pk_field_name: str | None = None
        #: The attribute names Model.__setattr__ does more than store - relations, their key columns,
        #: the soft-delete field and a generated primary key; None until worked out.
        self.setattr_hooks: frozenset[str] | None = None
        #: The ``__setattr__`` Model.__setattr__ stores a plain attribute with - set with
        #: ``setattr_hooks``.
        self.next_setattr: Callable[[Any, str, Any], None] = object.__setattr__
        #: The ``rust.native.rows.ModelConstructor`` setting up new instances; False when
        #: ``Model.__init__`` does it alone; None until worked out.
        self.instance_constructor: Any = None
        self.default_custom_generated_pk: bool = False
        self._model: type[Model] = None  # type: ignore[assignment]
        self.table_description: str = getattr(meta, ModelOption.TABLE_DESCRIPTION, "")
        self.table_options: tuple[TableOptions, ...] = SchemaObjectDeclarations.get_declared_table_options(meta)
        self.pk: Field[Any] = None  # type: ignore[assignment]
        self.db_pk_column: str = ""
        #: id(cache) -> this model's bucket of that Cache (the statement plans, the decode
        #: plans, the descriptions of its filter keys) - held here so the entries live exactly as
        #: long as the model class.
        self.plan_cache_buckets: dict[int, Any] = {}
        #: The model's plain buckets of the caches read on every query (``Cache.model_attribute``),
        #: None while the model has none: its parsed lookup paths, the field paths a path reads,
        #: the descriptions of its filter keys, ordering names and field paths' lookups, and its
        #: row layouts by (driver's native Python types, dialect).
        self.lookup_paths: dict[Any, Any] | None = None
        self.concrete_field_paths: dict[Any, Any] | None = None
        self.lookup_infos: dict[Any, Any] | None = None
        self.ordering_infos: dict[Any, Any] | None = None
        self.lookups_by_path: dict[Any, Any] | None = None
        self.hydration_layouts: dict[Any, Any] | None = None
        #: id(cache) -> this model's value of that ModelCache - held here so the value lives
        #: exactly as long as the model class.
        self.model_cache_values: dict[int, Any] = {}
        self.db_default_db_columns: tuple[str, ...] = ()

    def get_table_options(self, dialect: Dialect) -> TableOptions | None:
        """Returns the table options of one dialect.

        Args:
            dialect: The dialect.

        Returns:
            The ``Meta.table_options`` entry of the dialect, else the dialect's default options
            (``Dialect.default_table_options``), None when there are none.
        """
        return next(
            (options for options in self.table_options if options.dialect_name == dialect.name),
            dialect.default_table_options,
        )

    @property
    def full_name(self) -> str:
        """``"app.ModelName"`` - the label relations, ``swappable`` settings and
        ``HareContext.get_model()`` name the model by."""
        return f"{self.app}.{self._model.__name__}"

    @property
    def query_class(self) -> type[Query]:
        """The query class ``basequery`` is currently bound to - its dialect renders this model's
        SQL. Part of every cached-SQL key: SQL rendered while another context's builder was bound
        must never be served under this context's own dialect/connection."""
        return self.basequery.query_class

    @property
    def is_bound(self) -> bool:
        """Whether the model is bound - its relations resolved and every filter key and ordering
        name of it described - by ``Hare.bind_models()``, ``Hare.bind_models()`` or
        ``Hare.init()``. Until then the model can't describe a filter key or an ordering name."""
        return self._inited

    def _check_bound(self) -> None:
        """Refuses to describe a filter key or an ordering name of a model not bound yet.

        Raises:
            ConfigurationError: The model isn't bound.
        """
        if not self._inited:
            raise ConfigurationError(
                f"{self._model.__name__} is not bound yet: call Hare.bind_models() or Hare.init() "
                "before describing its filters or orderings"
            )

    @property
    def has_primary_key(self) -> bool:
        """Whether the model has a primary key - False with ``Meta.primary_key = None``, whose
        ``primary_key_attribute`` is ``()``."""
        return bool(self.primary_key_attribute)

    @property
    def has_composite_primary_key(self) -> bool:
        """Whether the primary key is composite (``CompositePrimaryKey``) - ``primary_key_attribute`` is then a
        tuple of field names, ``pk`` None and ``pk_fields`` the key's fields."""
        return isinstance(self.primary_key_attribute, tuple) and bool(self.primary_key_attribute)

    def get_with_blind_indexes(self, field_names: Iterable[str]) -> list[str]:
        """Field names to write, each encrypted field followed by its blind index - the index is
        written whenever its field is.

        Args:
            field_names: The names.

        Returns:
            The names, deduplicated, in order.
        """
        if not self.blind_index_fields:
            return list(field_names)
        names: dict[str, None] = {}
        for field_name in field_names:
            names[field_name] = None
            blind_index_field_name = self.blind_index_fields.get(field_name)
            if blind_index_field_name is not None:
                names[blind_index_field_name] = None
        return list(names)

    def raise_if_no_primary_key(self, operation: str) -> None:
        """Rejects an operation that identifies a row by its primary key on a model without one.

        Args:
            operation: What needs the key, named in the error (``"Model.save() of a fetched row"``).

        Raises:
            ConfigurationError: The model has no primary key.
        """
        if not self.has_primary_key:
            raise ConfigurationError(
                f"{self._model.__name__} has no primary key (Meta.primary_key = None) - {operation} needs "
                "one to identify a row; filter the queryset and use QuerySet.update()/delete() instead"
            )

    @property
    def primary_key_attribute_names(self) -> tuple[str, ...]:
        """``primary_key_attribute`` normalized to a tuple regardless of whether the PK is a single field or
        composite - lets code that only needs "which field names make up the PK" (membership
        checks, iteration) stay agnostic to which case it's in."""
        return (
            self.primary_key_attribute
            if isinstance(self.primary_key_attribute, tuple)
            else (self.primary_key_attribute,)
        )

    def get_column_names(self, field_names: Iterable[str]) -> list[str]:
        """Maps field names, as indexes and constraints name them, to DB column names. A relation maps
        to its key column(s); a name that is not a field is taken as a column name.

        Args:
            field_names: Field names, in order.

        Returns:
            The DB column names, in the same order.
        """
        column_names: list[str] = []
        for field_name in field_names:
            field_object = self.fields_map.get(field_name)
            if field_object is None:
                column_names.append(field_name)
            elif isinstance(field_object, ForeignKeyFieldInstance) and len(field_object.db_column_names) > 1:
                column_names.extend(field_object.db_column_names)
            elif isinstance(field_object, ForeignKeyFieldInstance):
                key_field_name = field_object.source_field or f"{field_name}{FOREIGN_KEY_COLUMN_SUFFIX}"
                key_field_object = self.fields_map.get(key_field_name)
                column_names.append(
                    key_field_object.source_field or key_field_name if key_field_object is not None else key_field_name
                )
            else:
                column_names.append(field_object.source_field or field_name)
        return column_names

    def get_field_index_columns(self) -> list[tuple[str, tuple[str, ...]]]:
        """The indexes the fields' own ``db_index=True`` implies, in column order.

        Returns:
            (field name, column names) pairs - a plain or shadow key field with its one column,
            a relation to a composite primary key with all of its key columns.
        """
        field_index_columns: list[tuple[str, tuple[str, ...]]] = []
        for field_name, column_name in self.fields_db_projection.items():
            field_object = self.fields_map[field_name]
            if field_object.index and not field_object.pk:
                field_index_columns.append((field_name, (column_name,)))
            relation_field = field_object.reference
            if (
                isinstance(relation_field, ForeignKeyFieldInstance)
                and relation_field.index
                and len(relation_field.source_fields) > 1
                and relation_field.source_fields[0] == field_name
            ):
                field_index_columns.append((relation_field.model_field_name, tuple(relation_field.db_column_names)))
        return field_index_columns

    def get_leading_index_entries(self) -> list[tuple[str, ...]]:
        """The field names of every index a lookup on its leading columns can use: plain btree
        ``Meta.indexes`` entries, unconditional UniqueConstraints and a composite primary key. An
        unnamed index over one foreign key is left out - it is that relation's own.

        Returns:
            Each entry's field (or column) names, in index order.
        """
        entries: list[tuple[str, ...]] = []
        for index in self.indexes:
            if isinstance(index, Index):
                if type(index) is not Index or not index.fields or index.expressions or index.opclasses:
                    continue
                if not index.name and not index.unique and self.is_relation_field_names(index.fields):
                    continue
                entries.append(tuple(index.fields))
            elif not self.is_relation_field_names(tuple(index)):
                entries.append(tuple(index))
        entries.extend(
            tuple(constraint.fields)
            for constraint in self.constraints
            if isinstance(constraint, UniqueConstraint) and constraint.fields and constraint.condition is None
        )
        if self.has_composite_primary_key:
            entries.append(cast("tuple[str, ...]", self.primary_key_attribute))
        return entries

    def is_relation_field_names(self, field_names: tuple[str, ...] | list[str]) -> bool:
        """Whether the names are exactly one foreign key field.

        Args:
            field_names: Field names of an index entry.

        Returns:
            True for a single ForeignKeyField name.
        """
        return len(field_names) == 1 and isinstance(self.fields_map.get(field_names[0]), ForeignKeyFieldInstance)

    def is_indexed_by_leading_columns(self, relation_field_name: str, key_names: list[tuple[str, ...]]) -> bool:
        """Whether a declared index already serves a relation's lookups, or would take the name of
        its own index.

        Args:
            relation_field_name: The relation field's name, which stands for all of its key columns.
            key_names: For each key column, the names an index entry can refer to it by.

        Returns:
            True when an entry of ``get_leading_index_entries()`` starts with all key columns in
            any order, or an unnamed index of another type (partial, GIN, ...) spans exactly the
            key columns in order - it gets the same generated name.
        """
        if any(
            self.leads_with_key_columns(entry_names, relation_field_name, key_names)
            for entry_names in self.get_leading_index_entries()
        ):
            return True
        for index in self.indexes:
            if not isinstance(index, Index) or type(index) is Index or index.name or index.unique:
                continue
            entry_names = tuple(index.fields or ())
            if entry_names == (relation_field_name,) or (
                len(entry_names) == len(key_names)
                and all(entry_name in names for entry_name, names in zip(entry_names, key_names, strict=True))
            ):
                return True
        return False

    @staticmethod
    def leads_with_key_columns(
        entry_names: tuple[str, ...], relation_field_name: str, key_names: list[tuple[str, ...]]
    ) -> bool:
        """Whether an index entry starts with all key columns of a relation, in any order.

        Args:
            entry_names: The index entry's field (or column) names, in order.
            relation_field_name: The relation field's name, which stands for all of its key columns.
            key_names: For each key column, the names an index entry can refer to it by.

        Returns:
            True when the leading entry names cover every key column.
        """
        uncovered_positions = set(range(len(key_names)))
        for entry_name in entry_names:
            if entry_name == relation_field_name:
                return True
            matched_positions = {position for position in uncovered_positions if entry_name in key_names[position]}
            if not matched_positions:
                return False
            uncovered_positions -= matched_positions
            if not uncovered_positions:
                return True
        return False

    def add_field(self, name: str, value: Field[Any]) -> None:
        if name in self.fields_map:
            raise ConfigurationError(f"Field {name} already present in meta")
        value.model = self._model
        self.fields_map[name] = value
        value.model_field_name = name

        if value.has_db_field:
            self.fields_db_projection[name] = value.source_field or name

        # Dropped, not recomputed: apps are registered incrementally, so a relation can arrive after
        # the deletion caches were filled - the transitive ones of other models included.
        if isinstance(value, ManyToManyFieldInstance):
            self.many_to_many_fields.add(name)
            Caches.forget_model_caches((self._model,))
        elif isinstance(value, OneToOneFieldInstance):
            self.one_to_one_fields.add(name)
            Caches.forget_model_caches((self._model,))
        elif isinstance(value, ForeignKeyFieldInstance):
            self.foreign_key_fields.add(name)
            Caches.forget_model_caches((self._model,))
        elif isinstance(value, BackwardOneToOneRelation):
            self.backward_one_to_one_fields.add(name)
            Caches.forget_model_caches((self._model,))
        elif isinstance(value, BackwardForeignKeyRelation):
            self.backward_foreign_key_fields.add(name)
            Caches.forget_model_caches((self._model,))

        LookupInfoBuilder.forget_descriptions()
        self.finalise_fields()

    def add_hidden_backward_relation(self, key: str, value: BackwardForeignKeyRelation[Any]) -> None:
        """Registers a backward relation that has no accessor on the model.

        Args:
            key: The forward field's ``"app.Model.field"``; re-registering a key replaces it.
            value: The backward relation.
        """
        value.model = self._model
        value.model_field_name = key
        self.hidden_backward_relations[key] = value
        LookupInfoBuilder.forget_descriptions()
        Caches.forget_model_caches((self._model,))

    def remove_field(self, name: str) -> None:
        """Reverses ``add_field()``: drops the field, its lookups and its class-level accessor.

        Args:
            name: The field's model field name.

        Raises:
            ConfigurationError: No field named ``name`` exists.
        """
        value = self.fields_map.pop(name, None)
        if value is None:
            raise ConfigurationError(f"Field {name} not present in meta")
        self.fields_db_projection.pop(name, None)
        self.many_to_many_fields.discard(name)
        self.backward_foreign_key_fields.discard(name)
        self.backward_one_to_one_fields.discard(name)
        self.foreign_key_fields.discard(name)
        self.one_to_one_fields.discard(name)
        self.foreign_key_shadow_columns.pop(name, None)
        LookupInfoBuilder.forget_descriptions()
        if isinstance(self._model.__dict__.get(name), property):
            delattr(self._model, name)
        Caches.forget_model_caches((self._model,))
        self.finalise_fields()

    def check_not_swapped(self) -> None:
        """
        Raises:
            ConfigurationError: This model is swapped for another one by its ``swappable`` setting.
        """
        if self.swapped is not None:
            raise ConfigurationError(
                f'"{self.full_name}" has been swapped for "{self.swapped}" by the {self.swappable} setting - '
                f'query "{self.swapped}" instead'
            )

    @property
    def connection(self) -> DatabaseClient:
        if self.default_connection is None:
            raise ConfigurationError(f"default_connection for the model {self._model} cannot be None")
        return Connections.get(self.default_connection)

    def check_tenant_client(self, client: DatabaseClient) -> None:
        """Checks a query of the model may run on a client - a ``Meta.tenant_schema`` model's on a
        tenant schema's client, a model with a ``TenantCondition`` policy's in a transaction that set
        its tenants. Called only when ``checks_tenant_client`` is set.

        Args:
            client: The client the query would run on.

        Raises:
            ConfigurationError: The connection has no ``tenant_schema_template`` /
                ``tenant_row_level_security``.
            QueryError: No single tenant is active, or the query runs outside a transaction that set
                its tenants.
        """
        if self.tenant_schema and client.tenant_schema is None:
            raise self.get_missing_tenant_schema_error(client)
        if self.tenant_row_level_security and not getattr(client, "sets_tenants", False):
            raise TenantRowLevelSecurity.get_missing_tenants_error(self._model, client)

    def get_missing_tenant_schema_error(self, client: DatabaseClient) -> HareError:
        """The error of reaching a ``Meta.tenant_schema`` model's table through a client of no tenant.

        Args:
            client: The client the model's connection gave.

        Returns:
            ConfigurationError when the connection has no schema per tenant, else QueryError - no
            single tenant is active.
        """
        if client.tenant_schema_template is None:
            return ConfigurationError(
                f"{self._model.__name__} has Meta.tenant_schema, but its connection "
                f"'{self.default_connection}' has no tenant_schema_template"
            )
        return QueryError(
            f"{self._model.__name__} has Meta.tenant_schema - its table is reached inside "
            "Tenancy.scope(tenant) of a single tenant"
        )

    @property
    def ordering(self) -> tuple[tuple[str, Order], ...]:
        if not self._ordering_validated:
            # Only the first segment of a path is checked, as finalise_fields() does; "pk" is always
            # known.
            unknown_fields = {
                field_path
                for field_path, _ in self._default_ordering
                if field_path != "pk" and field_path.partition("__")[0] not in self.fields
            }
            raise ConfigurationError(
                f"Unknown fields {','.join(unknown_fields)} in default ordering for model {self._model.__name__}"
            )
        if self._default_ordering and any(field_name == "pk" for field_name, _ in self._default_ordering):
            # Resolved here, not in finalise_fields(): self.primary_key_attribute is only final after
            # build_meta(). "pk" orders by every primary key column.
            self._default_ordering = tuple(
                (ordering_field_name, order)
                for field_name, order in self._default_ordering
                for ordering_field_name in (self.primary_key_attribute_names if field_name == "pk" else (field_name,))
            )
        return self._default_ordering

    def get_lookup_info(self, key: str) -> LookupInfo:
        """Describes a ``.filter()`` key of this model - the relations it crosses, the field it
        compares, the lookup and the value the lookup takes - without building a query. Descriptions
        are cached per key.

        Args:
            key: The filter key.

        Returns:
            The description.

        Raises:
            FieldError: The key names no field or relation, or a lookup its field doesn't have.
            QueryError: A lookup other than equality, membership or ``isnull`` on a relation to a
                composite key.
            ConfigurationError: The model isn't bound yet (``is_bound``).
        """
        self._check_bound()
        return self._get_lookup_info(key)

    def _get_lookup_info(self, key: str, label: str | None = None) -> LookupInfo:
        """``get_lookup_info()``, naming the key as ``label`` in an error.

        Args:
            key: The filter key.
            label: What the key is named as in an error, ``Unknown filter param '<key>'`` by
                default.

        Returns:
            The description.
        """
        lookup_infos: dict[str, LookupInfo] | None = self.lookup_infos
        if lookup_infos is None:
            lookup_infos = LookupInfoBuilder.lookup_infos.get_model_bucket(self._model)
        lookup_info = lookup_infos.get(key)
        if lookup_info is None:
            lookup_info = lookup_infos[key] = LookupInfoBuilder.get_lookup_info(self._model, key, label=label)
        return lookup_info

    def get_lookups(self, path: str, dialect: Dialect | None = None) -> dict[str, LookupInfo]:
        """Describes every lookup of a field path that a dialect runs.

        Args:
            path: A field or relation, after any relations (``author__name``, ``tags``, ``pk``).
            dialect: The dialect - by default the one of the connection a query on the model runs
                on now (``Model.get_connection()``).

        Returns:
            Each lookup's suffix after ``path`` (``""`` for plain equality, ``"icontains"``,
            ``"year__gte"``) to its description.

        Raises:
            FieldError: The path names no field or relation.
            ConfigurationError: The model isn't bound yet (``is_bound``).
        """
        self._check_bound()
        if dialect is None:
            dialect = self._model.get_connection().dialect
        lookups_by_path = self.lookups_by_path
        if lookups_by_path is None:
            lookups_by_path = LookupInfoBuilder.lookups_by_path.get_model_bucket(self._model)
        cache_key = (path, dialect.name)
        lookups = lookups_by_path.get(cache_key)
        if lookups is None:
            lookups = lookups_by_path[cache_key] = LookupInfoBuilder.get_lookups(self._model, path, dialect)
        return dict(lookups)

    def get_ordering_info(self, name: str) -> OrderingInfo:
        """Describes an ``.order_by()`` name of this model - the relations it crosses and the
        fields it orders by (every key field of a composite ``pk``, a forward relation's own key
        column(s)), and the direction.

        Args:
            name: The ordering name, optionally with a leading ``-``.

        Returns:
            The description.

        Raises:
            FieldError: The name names no field or relation.
            ConfigurationError: The model isn't bound yet (``is_bound``).
        """
        self._check_bound()
        ordering_infos: dict[str, OrderingInfo] | None = self.ordering_infos
        if ordering_infos is None:
            ordering_infos = LookupInfoBuilder.ordering_infos.get_model_bucket(self._model)
        ordering_info = ordering_infos.get(name)
        if ordering_info is None:
            ordering_info = ordering_infos[name] = LookupInfoBuilder.get_ordering_info(self._model, name)
        return ordering_info

    def finalise_model(self) -> None:
        """
        Finalise the model after it had been fully loaded.
        """
        self.finalise_fields()
        LookupInfoBuilder.forget_descriptions()
        LazyRelationFields.generate_lazy_foreign_key_and_many_to_many_fields(self)
        LazyRelationFields.generate_db_fields(self)
        ModelDefinitionChecks.validate_distinct_columns(self)
        ModelDefinitionChecks.get_index_expressions(self)
        ModelDefinitionChecks.validate_soft_delete_field(self)
        ModelDefinitionChecks.validate_tenant_field(self)
        ModelDefinitionChecks.validate_tenant_schema(self)
        ModelDefinitionChecks.validate_tenant_row_level_security(self)
        ModelDefinitionChecks.validate_policies_have_row_level_security(self)
        ModelDefinitionChecks.validate_optimistic_lock_field(self)
        ModelDefinitionChecks.validate_together_field_indexability(self)
        ModelDefinitionChecks.validate_unique_indexes_do_not_collide_with_constraints(self)
        ModelDefinitionChecks.validate_unnamed_unique_constraints_are_distinct(self)
        ModelDefinitionChecks.validate_identifier_lengths(self)
        self.change_capture_needs = ChangeCapturing.build_needs(self)
        self.setattr_hooks = None
        self.instance_constructor = None

    def get_setattr_hooks(self) -> frozenset[str]:
        """The attribute names ``Model.__setattr__`` does more than store, worked out once.

        Returns:
            The relations, their key columns, the soft-delete field and a generated primary key.
        """
        if self.setattr_hooks is None:
            # Imported here: the Model module imports this one.
            from hare.models.model import Model

            # What super().__setattr__() inside Model.__setattr__ reaches - object's, unless a class
            # after Model in the model's MRO defines its own.
            mro = self._model.__mro__
            self.next_setattr = next(
                klass.__dict__["__setattr__"]
                for klass in mro[mro.index(Model) + 1 :]
                if "__setattr__" in klass.__dict__
            )
            hooks = {*self.foreign_key_fields, *self.one_to_one_fields, *self.foreign_key_shadow_columns}
            if self.soft_delete_field is not None:
                hooks.add(self.soft_delete_field)
            if self.generated_pk_field_name is not None:
                hooks.add(self.generated_pk_field_name)
            self.setattr_hooks = frozenset(hooks)
        return self.setattr_hooks

    def get_instance_constructor(self) -> Any:
        """The constructor of the model's new instances, built once.

        Returns:
            The ``rust.native.rows.ModelConstructor``; False when ``Model.__init__`` sets instances
            up alone.
        """
        if self.instance_constructor is None:
            # Imported here: the module imports the Model module, which imports this one.
            from hare.models.class_building.instance_constructors import InstanceConstructors

            constructor = InstanceConstructors.build(self._model)
            if constructor is None:
                # Not known yet - worked out again on the next instance.
                return False
            self.instance_constructor = constructor
        return self.instance_constructor

    def finalise_fields(self) -> None:
        self.setattr_hooks = None
        self.instance_constructor = None
        self.db_fields = tuple(dict.fromkeys(self.fields_db_projection.values()))
        self.fields = set(self.fields_map.keys())
        self.fields_db_projection_reverse = {value: key for key, value in self.fields_db_projection.items()}
        self.fetch_fields = (
            self.many_to_many_fields
            | self.backward_foreign_key_fields
            | self.foreign_key_fields
            | self.backward_one_to_one_fields
            | self.one_to_one_fields
        )
        # Precomputed once here rather than recomputed on every DirtyFields.snapshot_dirty_fields()/
        # get_dirty_fields()/clone() call - fields_map/fetch_fields are both fixed once fields
        # are finalised, never per-instance.
        self.direct_fields = frozenset(self.fields_map.keys() - self.fetch_fields)
        # The fields read once for what each list below takes of them - every model's fields are
        # finalised again as relations are added.
        sensitive_fields: list[str] = []
        blind_index_fields: dict[str, str] = {}
        generated_fields: list[str] = []
        db_default_columns: list[str] = []
        fields_map = self.fields_map
        for name, field in fields_map.items():
            if field.sensitive:
                sensitive_fields.append(name)
            blind_index_source_field_name = getattr(field, "blind_index_source_field_name", None)
            if blind_index_source_field_name in fields_map:
                blind_index_fields[blind_index_source_field_name] = name
            if field.generated:
                generated_fields.append(field.source_field or field.model_field_name)
            elif field.has_db_default():
                db_default_columns.append(field.source_field or field.model_field_name)
        self.sensitive_fields = frozenset(sensitive_fields)
        self.blind_index_fields = blind_index_fields
        self.generated_db_fields = tuple(generated_fields)
        self.recomputed_db_fields = tuple(column for column in generated_fields if column != self.db_pk_column)
        # Computed once, not per hydrated row. A composite primary key is never generated.
        self.default_custom_generated_pk = not isinstance(self.primary_key_attribute, tuple) and (
            self.db_pk_column not in self.generated_db_fields
        )

        self.generated_pk_field_name = (
            self.primary_key_attribute
            if isinstance(self.primary_key_attribute, str) and self.pk is not None and self.pk.generated
            else None
        )

        self.db_default_db_columns = tuple(db_default_columns)

        self._ordering_validated = True
        for field_name, _ in self._default_ordering:
            # "pk" is never a real key in self.fields (only QuerySet.filter()/get() special-case
            # that alias) - resolving it to the real primary_key_attribute name is deferred to the `ordering`
            # property itself (see its own comment), so it must not be flagged unknown here.
            if field_name != "pk" and field_name.partition("__")[0] not in self.fields:
                self._ordering_validated = False
                break

    def get_hydration_layout(self, connection: DatabaseClient) -> HydrationLayout:
        """How the model's rows are read on ``connection``.

        Args:
            connection: The connection the rows are read on.

        Returns:
            The layout.
        """
        layouts: dict[tuple[frozenset[type], Dialect], HydrationLayout] | None = self.hydration_layouts
        if layouts is None:
            layouts = HydrationLayout.layouts.get_model_bucket(self._model)
        key = (connection.native_python_types, connection.dialect)
        layout = layouts.get(key)
        if layout is None:
            layout = layouts[key] = HydrationLayout.build(self, connection)
        return layout

    @staticmethod
    def _get_together(meta: type[Model.Meta] | None, together: str) -> tuple[tuple[str, ...], ...]:
        _together = getattr(meta, together, ())

        if _together and isinstance(_together, (list, tuple)) and isinstance(_together[0], str):
            _together = (_together,)

        # return without validation, validation will be done further in the code
        return _together

    @staticmethod
    def _get_latest_by_fields(meta: type[Model.Meta] | None) -> tuple[str, ...]:
        """``Meta.get_latest_by`` as a tuple of field names.

        Args:
            meta: The model's Meta.

        Returns:
            The names, ``-`` before a descending one; empty without the option.

        Raises:
            ConfigurationError: The option is neither a field name nor a sequence of them.
        """
        declared = getattr(meta, ModelOption.GET_LATEST_BY, ())
        names = (declared,) if isinstance(declared, str) else declared
        if not isinstance(names, (list, tuple)) or not all(isinstance(name, str) and name for name in names):
            raise ConfigurationError(
                f"Meta.get_latest_by must be a field name or a sequence of field names, got {declared!r}"
            )
        return tuple(names)

    @staticmethod
    def _prepare_default_ordering(
        meta: type[Model.Meta] | None,
    ) -> tuple[tuple[str, Order], ...]:
        ordering_list = getattr(meta, ModelOption.ORDERING, ())

        return tuple(QuerySet._get_ordering_string(ordering) for ordering in ordering_list)
