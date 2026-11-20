from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator
from typing import TYPE_CHECKING, Any

from hare.core.caching.cache import Cache
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
from hare.dialects.base.literals.constants import SQL_NULL_BYTE
from hare.exceptions import ConfigurationError, HareError, QueryError
from hare.fields.constants import FOREIGN_KEY_COLUMN_SUFFIX, SCHEMA_TEXT_NULL_BYTE_MESSAGE, SENSITIVE_VALUE_PLACEHOLDER
from hare.fields.field import Field
from hare.fields.generated_field import GeneratedField
from hare.fields.relations.fields.backward_foreign_key_relation import BackwardForeignKeyRelation
from hare.fields.relations.fields.declarations import BackwardOneToOneRelation
from hare.fields.relations.fields.foreign_key_field_instance import ForeignKeyFieldInstance
from hare.fields.relations.fields.generic_foreign_key_field_instance import GenericForeignKeyFieldInstance
from hare.fields.relations.fields.many_to_many_field_instance import ManyToManyFieldInstance
from hare.fields.relations.fields.one_to_one_field_instance import OneToOneFieldInstance
from hare.instrumentation.capture.change_capturing import ChangeCapturing
from hare.models.class_building.lazy_relation_fields import LazyRelationFields
from hare.models.class_building.model_definition_checks import ModelDefinitionChecks
from hare.models.constants import META_ORDERING_ALIAS_PREFIX, UNBOUND_MODEL_MESSAGE
from hare.models.enums import ModelOption
from hare.models.tenancy.tenant_connection_checks import TenantConnectionChecks
from hare.query.constants import PRIMARY_KEY_ALIAS
from hare.query.expressions.ordering import Ordering
from hare.query.lookup_info.lookup_info import LookupInfo
from hare.query.lookup_info.lookup_info_builder import LookupInfoBuilder
from hare.query.lookup_info.ordering_info import OrderingInfo
from hare.query.rows.native.hydration_layout import HydrationLayout
from hare.sql import Order, Query, Table
from hare.sql.builder.queries.query_builder import QueryBuilder

if TYPE_CHECKING:
    from hare.core.apps.model_binding_graph import ModelBindingGraph
    from hare.ddl.constraints.check_constraint import CheckConstraint
    from hare.ddl.constraints.exclusion_constraint import ExclusionConstraint
    from hare.ddl.constraints.foreign_key_constraint import ForeignKeyConstraint
    from hare.ddl.schema_objects.trigger import Trigger
    from hare.dialects.base.dialect import Dialect
    from hare.instrumentation.capture.capture_needs import CaptureNeeds
    from hare.instrumentation.capture.change_sink import ChangeSink
    from hare.models.class_building.generic_schema_binding import GenericSchemaBinding
    from hare.models.model import Model
    from hare.models.model_declaration import ModelDeclaration
    from hare.query.expressions.expression import Expression

    ModelConstraint = UniqueConstraint | CheckConstraint | ExclusionConstraint | ForeignKeyConstraint


class ModelBinding:
    """Prepared fields, relations, table identity and cached layouts in one model graph.

    Class declarations are retained separately; an application binding never replaces them.
    Runtime connections remain execution inputs, outside this structural object.
    """

    __slots__ = (
        "__weakref__",
        "declaration",
        "graph",
        "relation_accessors",
        "_default_ordering",
        "_foreign_key_or_one_to_one_inited",
        "_inited",
        "_model",
        "_ordering_validated",
        "ordering_expressions",
        "abstract",
        "app",
        "backward_foreign_key_fields",
        "backward_one_to_one_fields",
        "generic_foreign_key_fields",
        "generic_schema_binding",
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
        "computed_field_names",
        "generated_db_table",
        "generated_pk_field_name",
        "get_latest_by",
        "setattr_hooks",
        "next_setattr",
        "hidden_backward_relations",
        "indexes",
        "many_to_many_fields",
        "managed",
        "manager",
        "meta",
        "one_to_one_fields",
        "declared_field_names",
        PRIMARY_KEY_ALIAS,
        "primary_key_attribute",
        "pk_without_overlaps",
        "pk_fields",
        "recomputed_db_fields",
        "returning",
        "schema",
        "sensitive_fields",
        "blind_index_fields",
        "cache_buckets",
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

    def __init__(self, declaration: ModelDeclaration, graph: ModelBindingGraph) -> None:
        """Allocate runtime state only from an existing class declaration and graph."""
        meta = declaration.meta
        self.declaration = declaration
        self.graph = graph
        self.relation_accessors: dict[str, Any] = {}
        self._read_table_names(meta)
        self._read_schema_objects(meta)
        self._read_query_options(meta)
        self._init_fields(meta)
        self._read_row_options(meta)
        self._init_derived_state(meta)
        self._model = declaration.model
        self.abstract = declaration.abstract

    def __copy__(self) -> ModelBinding:
        return self

    def __deepcopy__(self, memo: dict[int, Any]) -> ModelBinding:
        memo[id(self)] = self
        return self

    def __reduce__(self) -> tuple[Any, tuple[Any, ...]]:
        """Serialize structural identity through the graph recipe, leaving cache buckets behind."""
        from hare.core.apps.model_binding_graph import ModelBindingGraph

        return ModelBindingGraph.get_saved_binding, (self.graph, self._model)

    @classmethod
    def from_declaration(cls, declaration: ModelDeclaration, graph: ModelBindingGraph) -> ModelBinding:
        """Prepare fresh runtime fields from one class declaration, without changing the class.

        The graph allocates every model before it links relations. This method does not
        select a connection, run defaults or inspect another registry's prepared fields.
        """
        from hare.fields.declaration_snapshot import DeclarationSnapshot
        from hare.models.class_building.generic_foreign_keys import GenericForeignKeys
        from hare.models.class_building.model_field_collection import ModelFieldCollection
        from hare.models.class_building.primary_key_declaration import PrimaryKeyDeclaration

        memo: dict[int, Any] = {}
        fields = {name: field.snapshot(memo=memo) for name, field in declaration.fields_map.items()}
        projection: dict[str, str] = {}
        foreign_keys, many_to_many, one_to_one = ModelFieldCollection.dispatch_fields(fields, projection)
        binding = cls(declaration, graph)
        binding.fields_map = fields
        binding.fields_db_projection = projection
        binding.foreign_key_fields = foreign_keys
        binding.one_to_one_fields = one_to_one
        binding.many_to_many_fields = many_to_many
        primary_key = declaration.primary_key_attribute
        binding.primary_key_attribute = primary_key
        binding.declared_field_names = declaration.declared_field_names
        if isinstance(primary_key, tuple):
            binding.pk_fields = tuple(
                fields[PrimaryKeyDeclaration.get_component_name(fields, name)] for name in primary_key
            )
        elif pk_field := fields.get(primary_key):
            binding.pk = pk_field
            binding.pk_fields = (pk_field,)
            binding.db_pk_column = pk_field.source_field or (
                f"{primary_key}_id" if isinstance(pk_field, OneToOneFieldInstance) else primary_key
            )
        binding.pk_without_overlaps = declaration.pk_without_overlaps
        binding.table_description = declaration.table_description
        binding.manager = declaration.manager
        # Every graph owns its mutable schema declarations, including nested predicates/options.
        schema_values = {
            option.value: getattr(binding, option.value)
            for option in (
                ModelOption.INDEXES,
                ModelOption.CONSTRAINTS,
                ModelOption.TRIGGERS,
                ModelOption.VIEWS,
                ModelOption.MATERIALIZED_VIEWS,
                ModelOption.DICTIONARIES,
                ModelOption.FUNCTIONS,
                ModelOption.SEQUENCES,
                ModelOption.POLICIES,
                ModelOption.GRANTS,
                ModelOption.TABLE_OPTIONS,
                ModelOption.CHANGE_CAPTURE,
            )
        }
        schema_values["ordering_expressions"] = binding.ordering_expressions
        for name, value in DeclarationSnapshot.copy_values(schema_values, {}).items():
            setattr(binding, name, value)
        for name, field in binding.fields_map.items():
            field.bind_to_model(declaration.model, name)
            field.get_binding().model_binding = binding
            if isinstance(field, GeneratedField):
                field.output_field.bind_to_model(declaration.model, name)
                field.output_field.get_binding().model_binding = binding
        generic_fields = {name: field.snapshot() for name, field in declaration.generic_foreign_key_fields.items()}
        GenericForeignKeys.bind(binding, generic_fields, declaration.abstract)
        for generic_field in generic_fields.values():
            generic_field._binding = binding
            for branch_name in generic_field.branch_names:
                if branch_name in fields:
                    fields[branch_name].generic_relation = generic_field  # type: ignore[attr-defined]
        binding.finalise_fields()
        return binding

    def _read_table_names(self, meta: type[Model.Meta] | None) -> None:
        """The options naming the model's table, and its manager.

        Args:
            meta: The model's Meta.
        """
        #: The model's Meta, the Meta of its abstract ancestors merged in - an extension reads its own
        #: options off it (an outbox model's ``Meta.outbox_wakeup``).
        self.meta = meta
        self.abstract = getattr(meta, ModelOption.ABSTRACT, False)
        self.manager = self.declaration.manager
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

    def _read_schema_objects(self, meta: type[Model.Meta] | None) -> None:
        """The schema objects the model declares besides its table.

        Args:
            meta: The model's Meta.
        """
        self.constraints: tuple[ModelConstraint, ...] = tuple(getattr(meta, ModelOption.CONSTRAINTS, ()))
        self.triggers: tuple[Trigger, ...] = tuple(getattr(meta, ModelOption.TRIGGERS, ()))
        self.views: tuple[View, ...] = tuple(getattr(meta, "views", ()))
        self.materialized_views: tuple[MaterializedView, ...] = tuple(getattr(meta, "materialized_views", ()))
        self.dictionaries: tuple[Dictionary, ...] = tuple(getattr(meta, "dictionaries", ()))
        self.functions: tuple[DatabaseFunction, ...] = tuple(getattr(meta, "functions", ()))
        self.sequences: tuple[DatabaseSequence, ...] = tuple(getattr(meta, "sequences", ()))
        self.policies: tuple[Policy, ...] = tuple(getattr(meta, "policies", ()))
        self.grants: tuple[Grant, ...] = tuple(getattr(meta, "grants", ()))
        self.row_level_security: RowLevelSecurity | None = getattr(meta, ModelOption.ROW_LEVEL_SECURITY, None)
        self.indexes: tuple[tuple[str, ...] | Index, ...] = getattr(meta, ModelOption.INDEXES, ())
        self.extensions: tuple[str, ...] = tuple(getattr(meta, ModelOption.EXTENSIONS, ()))

    def _read_query_options(self, meta: type[Model.Meta] | None) -> None:
        """The options of the model's swapping and of its queries' ordering.

        Args:
            meta: The model's Meta.
        """
        #: The ``swappable`` config setting a project may point at another model instead of this one.
        self.swappable: str | None = getattr(meta, ModelOption.SWAPPABLE, None)
        #: The ``app_label.ModelName`` label of the model the ``swappable`` setting points at
        #: instead of this one - None while this model is the one in use.
        self.swapped: str | None = None
        #: The expressions ``Meta.ordering`` orders by, by the alias the ordering names each by - a
        #: query ordered by one makes it an alias of its own (``AwaitableQuery``).
        self.ordering_expressions: dict[str, Expression] = {}
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
        self.generic_schema_binding: GenericSchemaBinding | None = None
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
        self.declared_field_names: frozenset[str] = frozenset()
        self._inited: bool = False
        self._foreign_key_or_one_to_one_inited: bool = False
        self.default_connection: str | None = None
        self.basequery: QueryBuilder = QueryBuilder()
        self.basequery_all_fields: QueryBuilder = QueryBuilder()
        self.basetable: Table = Table("")
        self.primary_key_attribute: str | tuple[str, ...] = getattr(meta, ModelOption.PRIMARY_KEY_ATTRIBUTE, "")
        #: The key conversion fields in key order: (pk,) for a scalar key, the component fields
        #: for a composite key, or () without a primary key. Updated as relations are linked.
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
        #: The fields the database computes on every write (``GeneratedField``) - no value of theirs
        #: is ever written, so the constructor refuses one.
        self.computed_field_names: frozenset[str] = frozenset()
        #: Name of the single-column primary key when the database generates it (autoincrement),
        #: else None - the only pk whose explicit assignment flips Model._custom_generated_pk.
        self.generated_pk_field_name: str | None = None
        #: The attribute names Model.__setattr__ does more than store - relations, their key columns,
        #: the soft-delete field and a generated primary key; None until worked out.
        self.setattr_hooks: frozenset[str] | None = None
        #: The ``__setattr__`` Model.__setattr__ stores a plain attribute with - set with
        #: ``setattr_hooks``.
        self.next_setattr: Callable[[Any, str, Any], None] = object.__setattr__
        #: The single-column primary key is supplied outside database generation.
        self.default_custom_generated_pk: bool = False
        self.table_description: str = getattr(meta, ModelOption.TABLE_DESCRIPTION, "")
        if isinstance(self.table_description, str) and SQL_NULL_BYTE in self.table_description:
            raise ConfigurationError(SCHEMA_TEXT_NULL_BYTE_MESSAGE.format(owner="Meta", parameter="table_description"))
        self.table_options: tuple[TableOptions, ...] = getattr(meta, ModelOption.TABLE_OPTIONS, ())
        self.pk: Field[Any] = None  # type: ignore[assignment]
        self.db_pk_column: str = ""
        #: Cache.bucket_id -> this model's bucket of that Cache (the statement plans, the decode
        #: plans, the descriptions of its filter keys) - retained by this graph node and
        #: released with the binding, independently of other graphs of the same model.
        self.cache_buckets: dict[int, Any] = {}
        self.db_default_db_columns: tuple[str, ...] = ()

    @property
    def forward_key_relation_names(self) -> set[str]:
        """The forward foreign keys and one-to-one relations with local key columns."""
        return self.foreign_key_fields | self.one_to_one_fields

    def get_table_options(self, dialect: Dialect) -> TableOptions | None:
        """Returns the table options of one dialect.

        Args:
            dialect: The dialect.

        Returns:
            The ``Meta.table_options`` entry of the dialect, else the dialect's default options
            (``Dialect.default_table_options``), None when there are none.
        """
        return dialect.get_table_options(self.table_options)

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
        name of it described - by ``Hare.bind_models()``, ``HareContext.bind_models()`` or
        ``Hare.init()``. Until then the model can't describe a filter key or an ordering name."""
        return self._inited

    def _check_bound(self) -> None:
        """Refuses to describe a filter key or an ordering name of a model not bound yet.

        Raises:
            ConfigurationError: The model isn't bound.
        """
        if not self._inited:
            raise ConfigurationError(UNBOUND_MODEL_MESSAGE.format(model_name=self._model.__name__))

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
            elif isinstance(field_object, ForeignKeyFieldInstance):
                relation_column_names = field_object.db_column_names
                if len(relation_column_names) > 1:
                    column_names.extend(relation_column_names)
                else:
                    key_field_name = field_object.source_field or f"{field_name}{FOREIGN_KEY_COLUMN_SUFFIX}"
                    key_field_object = self.fields_map.get(key_field_name)
                    column_names.append(
                        key_field_object.source_field or key_field_name
                        if key_field_object is not None
                        else key_field_name
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
                and len(source_fields := relation_field.source_fields) > 1
                and source_fields[0] == field_name
            ):
                field_index_columns.append((relation_field.model_field_name, relation_field.db_column_names))
        return field_index_columns

    def iter_field_unique_columns(self) -> Iterator[tuple[str, str]]:
        """Yield the field and column names for single-column UNIQUE constraints in projection order."""
        for field_name, column in self.fields_db_projection.items():
            field = self.fields_map[field_name]
            reference = getattr(field, "reference", None)
            # A key field of a relation to a composite key is unique only with the others.
            if field.unique and not field.pk and not (reference is not None and len(reference.to_field_instances) > 1):
                yield field_name, column

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
                if type(index) is not Index or not index.fields or index.declared_expressions or index.opclasses:
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
            entries.append(self.primary_key_attribute)  # type: ignore[arg-type]
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
        """Whether a declared index already serves a relation's lookups.

        Args:
            relation_field_name: The relation field's name, which stands for all of its key columns.
            key_names: For each key column, the names an index entry can refer to it by.

        Returns:
            True when an entry of ``get_leading_index_entries()`` starts with all key columns in
            any order.
        """
        return any(
            self.leads_with_key_columns(entry_names, relation_field_name, key_names)
            for entry_names in self.get_leading_index_entries()
        )

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
        """Adds a field to the model under a name, sorting it into the sets of its type.

        Args:
            name: The field's attribute name.
            value: The field.

        Raises:
            ConfigurationError: The model already has a field of that name.
        """
        if self.graph.sealed:
            raise ConfigurationError("A published model graph cannot be modified; prepare a new registry version")
        if name in self.fields_map:
            raise ConfigurationError(f"Field {name} already present in meta")
        value.bind_to_model(self._model, name)
        value.get_binding().model_binding = self
        self.fields_map[name] = value

        if value.has_db_field:
            self.fields_db_projection[name] = value.source_field or name

        if isinstance(value, ManyToManyFieldInstance):
            self.many_to_many_fields.add(name)
        elif isinstance(value, OneToOneFieldInstance):
            self.one_to_one_fields.add(name)
        elif isinstance(value, ForeignKeyFieldInstance):
            self.foreign_key_fields.add(name)
        elif isinstance(value, BackwardOneToOneRelation):
            self.backward_one_to_one_fields.add(name)
        elif isinstance(value, BackwardForeignKeyRelation):
            self.backward_foreign_key_fields.add(name)

        Cache.clear_holder(self)
        self.finalise_fields()

    def add_hidden_backward_relation(self, key: str, value: BackwardForeignKeyRelation[Any]) -> None:
        """Registers a backward relation that has no accessor on the model.

        Args:
            key: The forward field's ``"app.Model.field"``; re-registering a key replaces it.
            value: The backward relation.
        """
        if self.graph.sealed:
            raise ConfigurationError("A published model graph cannot be modified; prepare a new registry version")
        value.bind_to_model(self._model, key)
        value.get_binding().model_binding = self
        self.hidden_backward_relations[key] = value
        Cache.clear_holder(self)

    def place_key_columns(self) -> None:
        """Puts the key columns of each relation where the relation is declared among the columns -
        the table holds them there."""
        key_names_by_relation: dict[str, list[str]] = {}
        for name in self.fields_db_projection:
            reference = getattr(self.fields_map[name], "reference", None)
            if reference is not None:
                key_names_by_relation.setdefault(reference.model_field_name, []).append(name)
        columns: dict[str, str] = {}
        for name, field in self.fields_map.items():
            if name in key_names_by_relation:
                columns.update(
                    (key_name, self.fields_db_projection[key_name]) for key_name in key_names_by_relation[name]
                )
            elif name in self.fields_db_projection and getattr(field, "reference", None) is None:
                columns[name] = self.fields_db_projection[name]
        if len(columns) != len(self.fields_db_projection):
            # A relation isn't among the fields - the columns stay where they are.
            return
        self.fields_db_projection.clear()
        self.fields_db_projection.update(columns)

    def remove_field(self, name: str) -> None:
        """Reverses ``add_field()``: drops the field, its lookups and its class-level accessor.

        Args:
            name: The field's model field name.

        Raises:
            ConfigurationError: No field named ``name`` exists.
        """
        if self.graph.sealed:
            raise ConfigurationError("A published model graph cannot be modified; prepare a new registry version")
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
        self.relation_accessors.pop(name, None)
        Cache.clear_holder(self)
        self.finalise_fields()

    def check_not_swapped(self) -> None:
        """
        Raises:
            ConfigurationError: This model is swapped for another one by its ``swappable`` setting.
        """
        if self.swapped is not None:
            raise ConfigurationError(
                f"{self.full_name!r} has been swapped for {self.swapped!r} by the {self.swappable} setting - "
                f"query {self.swapped!r} instead"
            )

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
        return TenantConnectionChecks.check_tenant_client(self, client)

    def get_missing_tenant_schema_error(self, client: DatabaseClient) -> HareError:
        """The error of reaching a ``Meta.tenant_schema`` model's table through a client of no tenant.

        Args:
            client: The client the model's connection gave.

        Returns:
            ConfigurationError when the connection has no schema per tenant, else QueryError - no
            single tenant is active.
        """
        return TenantConnectionChecks.get_missing_tenant_schema_error(self, client)

    @property
    def ordering(self) -> tuple[tuple[str, Order], ...]:
        """``Meta.ordering`` as field paths and their directions, ``pk`` replaced by every primary key
        column.

        Raises:
            ConfigurationError: The ordering names a field the model lacks.
        """
        if not self._ordering_validated:
            # Only the first segment of a path is checked, as finalise_fields() does; "pk" is always
            # known.
            unknown_fields = {
                field_path
                for field_path, _ in self._default_ordering
                if field_path != PRIMARY_KEY_ALIAS
                and field_path not in self.ordering_expressions
                and field_path.partition("__")[0] not in self.fields
            }
            raise ConfigurationError(
                f"Unknown fields {','.join(unknown_fields)} in default ordering for model {self._model.__name__}"
            )
        if self._default_ordering and any(field_name == PRIMARY_KEY_ALIAS for field_name, _ in self._default_ordering):
            # Resolved here, not in finalise_fields(): self.primary_key_attribute is only final after
            # class construction. "pk" orders by every primary key column.
            self._default_ordering = tuple(
                (ordering_field_name, order)
                for field_name, order in self._default_ordering
                for ordering_field_name in (
                    self.primary_key_attribute_names if field_name == PRIMARY_KEY_ALIAS else (field_name,)
                )
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
        lookup_infos = LookupInfoBuilder.lookup_infos.get_bucket(self)
        lookup_info = lookup_infos.get(key)
        if lookup_info is None:
            lookup_info = lookup_infos.store(key, LookupInfoBuilder.get_lookup_info(self, key, label=label))
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
            dialect = self.graph.get_dialect(self.default_connection)
        lookups_by_path = LookupInfoBuilder.lookups_by_path.get_bucket(self)
        cache_key = (path, dialect)
        lookups = lookups_by_path.get(cache_key)
        if lookups is None:
            lookups = lookups_by_path.store(cache_key, LookupInfoBuilder.get_lookups(self, path, dialect))
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
        ordering_infos = LookupInfoBuilder.ordering_infos.get_bucket(self)
        ordering_info = ordering_infos.get(name)
        if ordering_info is None:
            ordering_info = ordering_infos.store(name, LookupInfoBuilder.get_ordering_info(self, name))
        return ordering_info

    def finalise_model(self) -> None:
        """Completes the model once every model is registered and its relations are wired: the field
        sets, the relation fields declared lazily, the table options naming relation keys, and the checks
        of the definition that need the fields."""
        if self.graph.sealed:
            raise ConfigurationError("A published model graph cannot be modified; prepare a new registry version")
        self.finalise_fields()
        Cache.clear_holder(self)
        LazyRelationFields.generate_lazy_foreign_key_and_many_to_many_fields(self)
        self.views = tuple(view.for_binding(self) for view in self.views)
        self.materialized_views = tuple(view.for_binding(self) for view in self.materialized_views)
        self.table_options = tuple(options.with_relation_keys(self) for options in self.table_options)
        ModelDefinitionChecks.validate_distinct_columns(self)
        ModelDefinitionChecks.validate_triggers_fire_on_a_table(self)
        ModelDefinitionChecks.check_index_expressions(self)
        ModelDefinitionChecks.validate_declarations_read_model_columns(self)
        ModelDefinitionChecks.validate_soft_delete_field(self)
        ModelDefinitionChecks.validate_tenant_field(self)
        ModelDefinitionChecks.validate_schema(self)
        ModelDefinitionChecks.validate_tenant_schema(self)
        ModelDefinitionChecks.validate_tenant_row_level_security(self)
        ModelDefinitionChecks.validate_policies_have_row_level_security(self)
        ModelDefinitionChecks.validate_field_names(self)
        ModelDefinitionChecks.validate_primary_key_values(self)
        ModelDefinitionChecks.validate_latest_by_fields(self)
        ModelDefinitionChecks.validate_optimistic_lock_field(self)
        ModelDefinitionChecks.validate_index_fields(self)
        ModelDefinitionChecks.validate_unique_indexes_do_not_collide_with_constraints(self)
        ModelDefinitionChecks.validate_unnamed_unique_constraints_are_distinct(self)
        ModelDefinitionChecks.validate_unnamed_indexes_are_distinct(self)
        ModelDefinitionChecks.validate_declared_names_are_distinct(self)
        ModelDefinitionChecks.validate_identifiers(self)
        self.change_capture_needs = ChangeCapturing.build_needs(self)
        self.setattr_hooks = None

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
            hooks = {*self.relation_accessors, *self.foreign_key_shadow_columns}
            if self.soft_delete_field is not None:
                hooks.add(self.soft_delete_field)
            if self.generated_pk_field_name is not None:
                hooks.add(self.generated_pk_field_name)
            self.setattr_hooks = frozenset(hooks)
        return self.setattr_hooks

    def finalise_fields(self) -> None:
        """Computes what is derived from the model's fields once they are all known: the columns, the
        field sets by type, and whether ``Meta.ordering`` names only fields the model has."""
        self.setattr_hooks = None
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
        computed_field_names: list[str] = []
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
                if not field.pk:
                    computed_field_names.append(name)
            elif field.has_db_default():
                db_default_columns.append(field.source_field or field.model_field_name)
        self.sensitive_fields = frozenset(sensitive_fields)
        self.blind_index_fields = blind_index_fields
        self.generated_db_fields = tuple(generated_fields)
        self.computed_field_names = frozenset(computed_field_names)
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
            if (
                field_name != PRIMARY_KEY_ALIAS
                and field_name not in self.ordering_expressions
                and field_name.partition("__")[0] not in self.fields
            ):
                self._ordering_validated = False
                break

    def get_hydration_layout(self, connection: DatabaseClient) -> HydrationLayout:
        """How the model's rows are read on ``connection``.

        Args:
            connection: The connection the rows are read on.

        Returns:
            The layout.
        """
        layouts = HydrationLayout.layouts.get_bucket(self)
        key = (connection.native_python_types, connection.dialect)
        layout = layouts.get(key)
        if layout is None:
            layout = layouts[key] = HydrationLayout.build(self, connection)
        return layout

    @staticmethod
    def _get_latest_by_fields(meta: type[Model.Meta] | None) -> tuple[str, ...]:
        """``Meta.get_latest_by`` as a tuple of field names.

        Args:
            meta: The model's Meta.

        Returns:
            The names, ``-`` before a descending one; empty without the option.
        """
        declared = getattr(meta, ModelOption.GET_LATEST_BY, ())
        return (declared,) if isinstance(declared, str) else tuple(declared)

    def get_composite_primary_key_parts(self, value: Any) -> Iterator[tuple[str, Any]]:
        """Validate and split a composite key before assigning its fields."""
        names = self.primary_key_attribute_names
        if not isinstance(value, tuple) or len(value) != len(names):
            raise QueryError(
                f"Composite pk must be set to a {len(names)}-tuple matching "
                f"{names}, got {self.get_shown_primary_key(value)}"
            )
        return zip(names, value, strict=True)

    def get_shown_primary_key(self, value: Any) -> str:
        """Format a supplied key using each component's diagnostic policy.

        A malformed composite value cannot be split safely and is hidden entirely
        when any component is sensitive. Non-sensitive keys retain their str form.
        """
        fields = self.pk_fields
        if not any(field.sensitive for field in fields):
            return str(value)
        if not self.has_composite_primary_key:
            return fields[0].get_value_for_message(value)
        if not isinstance(value, tuple) or len(value) != len(fields):
            return SENSITIVE_VALUE_PLACEHOLDER
        parts = (field.get_value_for_message(part) for field, part in zip(fields, value, strict=True))
        return f"({', '.join(parts)})"

    def _prepare_default_ordering(self, meta: type[Model.Meta] | None) -> tuple[tuple[str, Order], ...]:
        """``Meta.ordering`` as names and directions - an expression named by an alias of its own,
        kept in ``ordering_expressions``.

        Args:
            meta: The model's ``Meta``.

        Returns:
            The names and directions.
        """
        orderings: list[tuple[str, Order]] = []
        for ordering in getattr(meta, ModelOption.ORDERING, ()):
            target, order = Ordering.get_parts(ordering)
            if not isinstance(target, str):
                alias = f"{META_ORDERING_ALIAS_PREFIX}{len(self.ordering_expressions)}"
                self.ordering_expressions[alias] = target
                target = alias
            orderings.append((target, order))
        return tuple(orderings)
