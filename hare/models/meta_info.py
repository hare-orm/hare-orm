from __future__ import annotations

import inspect
from collections.abc import Callable, Iterable
from functools import partial
from typing import TYPE_CHECKING, Any, cast

from hare.core.caches import Caches
from hare.core.connections import Connections
from hare.core.constants import SWAPPABLE_SETTING_NAME_PATTERN
from hare.ddl.constraints.unique_constraint import UniqueConstraint
from hare.ddl.indexes.index import Index
from hare.ddl.table_options import TableOptions
from hare.dialects.base.client.database_client import DatabaseClient
from hare.dialects.constants import IDENTIFIER_LENGTH_LIMIT
from hare.exceptions import ConfigurationError
from hare.fields.base.field import Field
from hare.fields.constants import FK_COLUMN_SUFFIX
from hare.fields.data.numeric.int_field import IntField
from hare.fields.data.temporal.datetime_field import DatetimeField
from hare.fields.relations.fields.backward_fk_relation import BackwardFKRelation
from hare.fields.relations.fields.backward_one_to_one_relation import BackwardOneToOneRelation
from hare.fields.relations.fields.foreign_key_field_instance import ForeignKeyFieldInstance
from hare.fields.relations.fields.many_to_many_field_instance import ManyToManyFieldInstance
from hare.fields.relations.fields.one_to_one_field_instance import OneToOneFieldInstance
from hare.models.constants import UNSUPPORTED_META_OPTIONS
from hare.models.enums import ModelOption
from hare.query.lookup_info.lookup_info import LookupInfo
from hare.query.lookup_info.lookup_info_builder import LookupInfoBuilder
from hare.query.lookup_info.ordering_info import OrderingInfo
from hare.query.manager import Manager
from hare.query.queryset import QuerySet, QuerySetSingle
from hare.query.queryset.relations.reverse_relation import ReverseRelation
from hare.query.rows.hydration_layout import HydrationLayout
from hare.sql import Order, Query, Table

if TYPE_CHECKING:
    from hare.ddl.constraints.check_constraint import CheckConstraint
    from hare.ddl.constraints.exclusion_constraint import ExclusionConstraint
    from hare.ddl.constraints.foreign_key_constraint import ForeignKeyConstraint
    from hare.ddl.constraints.unique_constraint import UniqueConstraint
    from hare.ddl.triggers import Trigger
    from hare.dialects.base.dialect import Dialect
    from hare.models.model import Model

    ModelConstraint = UniqueConstraint | CheckConstraint | ExclusionConstraint | ForeignKeyConstraint
from hare.models.fk_setter_kwargs import FkSetterKwargs


class MetaInfo:
    __slots__ = (
        "_default_ordering",
        "_fk_o2o_inited",
        "_inited",
        "_model",
        "_ordering_validated",
        "abstract",
        "app",
        "backward_fk_fields",
        "backward_o2o_fields",
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
        "fk_fields",
        "fk_shadow_columns",
        "generated_db_fields",
        "generated_db_table",
        "generated_pk_field_name",
        "setattr_hooks",
        "next_setattr",
        "instance_constructor",
        "hidden_backward_relations",
        "indexes",
        "m2m_fields",
        "managed",
        "manager",
        "o2o_fields",
        "own_field_names",
        "pk",
        "pk_attr",
        "pk_fields",
        "recomputed_db_fields",
        "returning",
        "schema",
        "sensitive_fields",
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
        "track_dirty_fields",
        "triggers",
        "optimistic_lock_field",
    )

    def __init__(self, meta: type[Model.Meta] | None) -> None:
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
        self._raise_if_unsupported_option_declared(meta)
        self.constraints: tuple[ModelConstraint, ...] = tuple(getattr(meta, ModelOption.CONSTRAINTS, ()))
        self.triggers: tuple[Trigger, ...] = tuple(getattr(meta, ModelOption.TRIGGERS, ()))
        self.indexes: tuple[tuple[str, ...] | Index, ...] = self._get_together(meta, ModelOption.INDEXES)
        self.extensions: tuple[str, ...] = tuple(getattr(meta, ModelOption.EXTENSIONS, ()))
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
        self.fields: set[str] = set()
        #: Every column of the table, in the order the fields are declared - the SELECT column list.
        self.db_fields: tuple[str, ...] = ()
        self.m2m_fields: set[str] = set()
        self.fk_fields: set[str] = set()
        #: Key column field name ("author_id") -> the attribute caching its relation's related
        #: object ("_author") - assigning the column drops that cache.
        self.fk_shadow_columns: dict[str, str] = {}
        self.o2o_fields: set[str] = set()
        self.backward_fk_fields: set[str] = set()
        self.backward_o2o_fields: set[str] = set()
        #: Backward FK/O2O relations of forward fields declared with ``related_name=False``, keyed
        #: by the forward field's ``"app.Model.field"`` - no accessor, filter or fetch field, only
        #: seen by the ``on_delete`` cascade.
        self.hidden_backward_relations: dict[str, BackwardFKRelation[Any]] = {}
        self.fetch_fields: set[str] = set()
        self.direct_fields: frozenset[str] = frozenset()
        #: Names of every field declared (or, for an FK/O2O shadow column, inherited from its
        #: relation) with ``sensitive=True``.
        self.sensitive_fields: frozenset[str] = frozenset()
        self.fields_db_projection: dict[str, str] = {}
        self.fields_db_projection_reverse: dict[str, str] = {}
        self.fields_map: dict[str, Field[Any]] = {}
        #: The fields this class's own body declares, a synthesized `id` included - read by a
        #: subclass resolving a diamond.
        self.own_field_names: frozenset[str] = frozenset()
        self._inited: bool = False
        self._fk_o2o_inited: bool = False
        self.default_connection: str | None = None
        self.basequery: Query = Query()
        self.basequery_all_fields: Query = Query()
        self.basetable: Table = Table("")
        self.pk_attr: str | tuple[str, ...] = getattr(meta, ModelOption.PK_ATTR, "")
        #: Only set when pk_attr is a tuple (composite PK) - the actual Field objects, in the
        #: same order as pk_attr. Empty for a single-column PK (use .pk instead).
        self.pk_fields: tuple[Field[Any], ...] = ()
        self.soft_delete_field: str | None = getattr(meta, ModelOption.SOFT_DELETE_FIELD, None)
        # Whether a soft delete of the model's rows really deletes the cascaded rows that can't be
        # soft-deleted themselves and the M2M through rows, instead of keeping them for restore().
        self.soft_delete_hard_cascade: bool = getattr(meta, ModelOption.SOFT_DELETE_HARD_CASCADE, False)
        # The column holding the tenant of a multi-tenant model - of any field type.
        self.tenant_field: str | None = getattr(meta, ModelOption.TENANT_FIELD, None)
        self.optimistic_lock_field: str | None = getattr(meta, ModelOption.OPTIMISTIC_LOCK_FIELD, None)
        # Opt-in: an unconditional per-row snapshot would cost every hydration, not just the
        # models that actually use .get_dirty_fields() - see ModelColumns.compile_hydrate_function().
        self.track_dirty_fields: bool = getattr(meta, ModelOption.TRACK_DIRTY_FIELDS, False)
        # False: the table is never hare's to create, alter or drop - migrations and drift leave it
        # alone.
        self.managed: bool = getattr(meta, ModelOption.MANAGED, True)
        # The default of bulk_create()/bulk_update()'s `returning` when a call doesn't pass it.
        self.returning: bool = getattr(meta, ModelOption.RETURNING, False)
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
        self.table_options: tuple[TableOptions, ...] = self._get_table_options(meta)
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

    @staticmethod
    def _get_table_options(meta: type[Model.Meta] | None) -> tuple[TableOptions, ...]:
        """Reads and checks ``Meta.table_options``.

        Args:
            meta: The model's Meta.

        Returns:
            The options, one entry per dialect.

        Raises:
            ConfigurationError: An entry isn't a ``TableOptions``, or two are for one dialect.
        """
        table_options = tuple(getattr(meta, ModelOption.TABLE_OPTIONS, ()))
        dialect_names: set[str] = set()
        for options in table_options:
            if not isinstance(options, TableOptions):
                raise ConfigurationError(
                    f"Meta.table_options entries must be TableOptions of a dialect, got {options!r}"
                )
            if options.dialect_name in dialect_names:
                raise ConfigurationError(f"Meta.table_options has two entries for the {options.dialect_name} dialect")
            dialect_names.add(options.dialect_name)
        return table_options

    def get_table_options(self, dialect: Dialect) -> TableOptions | None:
        """Returns the table options of one dialect.

        Args:
            dialect: The dialect.

        Returns:
            The ``Meta.table_options`` entry of the dialect, None when it has none.
        """
        return next((options for options in self.table_options if options.dialect_name == dialect.name), None)

    @property
    def full_name(self) -> str:
        """``"app.ModelName"`` - the label relations, ``swappable`` settings and
        ``HareContext.get_model()`` name the model by."""
        return f"{self.app}.{self._model.__name__}"

    @property
    def query_builder_class(self) -> type[Query]:
        """The query builder class ``basequery`` is currently bound to - what renders this model's
        SQL. Part of every cached-SQL key: SQL rendered while another context's builder was bound
        must never be served under this context's own dialect/connection."""
        return type(self.basequery)

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
        ``pk_attr`` is ``()``."""
        return bool(self.pk_attr)

    @property
    def has_composite_primary_key(self) -> bool:
        """Whether the primary key is composite (``CompositePrimaryKey``) - ``pk_attr`` is then a
        tuple of field names, ``pk`` None and ``pk_fields`` the key's fields."""
        return isinstance(self.pk_attr, tuple) and bool(self.pk_attr)

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
    def pk_attr_names(self) -> tuple[str, ...]:
        """``pk_attr`` normalized to a tuple regardless of whether the PK is a single field or
        composite - lets code that only needs "which field names make up the PK" (membership
        checks, iteration) stay agnostic to which case it's in."""
        return self.pk_attr if isinstance(self.pk_attr, tuple) else (self.pk_attr,)

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
                key_field_name = field_object.source_field or f"{field_name}{FK_COLUMN_SUFFIX}"
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
        for constraint in self.constraints:
            if isinstance(constraint, UniqueConstraint) and constraint.fields and constraint.condition is None:
                entries.append(tuple(constraint.fields))
        if self.has_composite_primary_key:
            entries.append(cast("tuple[str, ...]", self.pk_attr))
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
            self.m2m_fields.add(name)
            Caches.forget_model_caches((self._model,))
        elif isinstance(value, BackwardOneToOneRelation):
            self.backward_o2o_fields.add(name)
            Caches.forget_model_caches((self._model,))
        elif isinstance(value, BackwardFKRelation):
            self.backward_fk_fields.add(name)
            Caches.forget_model_caches((self._model,))

        LookupInfoBuilder.forget_descriptions()
        self.finalise_fields()

    def add_hidden_backward_relation(self, key: str, value: BackwardFKRelation[Any]) -> None:
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
        self.m2m_fields.discard(name)
        self.backward_fk_fields.discard(name)
        self.backward_o2o_fields.discard(name)
        self.fk_fields.discard(name)
        self.o2o_fields.discard(name)
        self.fk_shadow_columns.pop(name, None)
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
    def db(self) -> DatabaseClient:
        if self.default_connection is None:
            raise ConfigurationError(f"default_connection for the model {self._model} cannot be None")
        return Connections.get(self.default_connection)

    @property
    def ordering(self) -> tuple[tuple[str, Order], ...]:
        if not self._ordering_validated:
            # Only the first segment of a path is checked, as finalise_fields() does; "pk" is always
            # known.
            unknown_fields = {
                f for f, _ in self._default_ordering if f != "pk" and f.partition("__")[0] not in self.fields
            }
            raise ConfigurationError(
                f"Unknown fields {','.join(unknown_fields)} in default ordering for model {self._model.__name__}"
            )
        if self._default_ordering and any(field_name == "pk" for field_name, _ in self._default_ordering):
            # Resolved here, not in finalise_fields(): self.pk_attr is only final after
            # build_meta(). "pk" orders by every primary key column.
            self._default_ordering = tuple(
                (ordering_field_name, order)
                for field_name, order in self._default_ordering
                for ordering_field_name in (self.pk_attr_names if field_name == "pk" else (field_name,))
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
        self._generate_lazy_fk_m2m_fields()
        self._generate_db_fields()
        self._validate_distinct_columns()
        self._get_index_expressions()
        self._validate_soft_delete_field()
        self._validate_tenant_field()
        self._validate_optimistic_lock_field()
        self._validate_together_field_indexability()
        self._validate_unique_indexes_do_not_collide_with_constraints()
        self._validate_unnamed_unique_constraints_are_distinct()
        self._validate_identifier_lengths()
        self.setattr_hooks = None
        self.instance_constructor = None

    def _validate_identifier_lengths(self) -> None:
        """Rejects a table, column, index or constraint name Postgres would silently truncate -
        two such names can end up the same identifier, and the schema never matches the model.

        Raises:
            ConfigurationError: A name is longer than Postgres's identifier limit.
        """
        names: list[tuple[str, str]] = [("the table", self.db_table)]
        names += [
            (f"the column of {field_name!r}", column) for field_name, column in self.fields_db_projection.items()
        ]
        for field_name in self.m2m_fields:
            m2m_field = self.fields_map[field_name]
            through = getattr(m2m_field, "through", None)
            if isinstance(through, str):
                names.append((f"the through table of {field_name!r}", through))
            for key in (getattr(m2m_field, "forward_key", None), getattr(m2m_field, "backward_key", None)):
                if isinstance(key, str):
                    names.append((f"the through table column of {field_name!r}", key))
        names += [("the index", index.name) for index in self.indexes if isinstance(index, Index) and index.name]
        names += [
            ("the constraint", constraint_name)
            for constraint in self.constraints
            if (constraint_name := getattr(constraint, "name", None))
        ]
        for description, name in names:
            if len(name.encode()) > IDENTIFIER_LENGTH_LIMIT:
                raise ConfigurationError(
                    f"{self._model.__name__}: {description} {name!r} is {len(name.encode())} bytes - longer than "
                    f"{IDENTIFIER_LENGTH_LIMIT} bytes, the identifier limit of a registered dialect, which would "
                    "truncate or reject it. Give it a shorter name."
                )

    def _validate_distinct_columns(self) -> None:
        """Rejects two fields that map to the same database column.

        Raises:
            ConfigurationError: If two fields share a column.
        """
        field_name_by_column_name: dict[str, str] = {}
        for field_name, column_name in self.fields_db_projection.items():
            reference = self.fields_map[field_name].reference
            owner_field_name = reference.model_field_name if reference is not None else field_name
            other_field_name = field_name_by_column_name.setdefault(column_name, owner_field_name)
            if other_field_name != owner_field_name:
                raise ConfigurationError(
                    f"Fields '{other_field_name}' and '{owner_field_name}' on {self._model.__name__} both map "
                    f"to the database column '{column_name}' - give one of them a different source_field"
                )

    def _validate_soft_delete_field(self) -> None:
        if not isinstance(self.soft_delete_hard_cascade, bool):
            raise ConfigurationError(f"Meta.soft_delete_hard_cascade on {self._model.__name__} must be a bool")
        if self.soft_delete_field is None:
            if self.soft_delete_hard_cascade:
                raise ConfigurationError(
                    f"Meta.soft_delete_hard_cascade on {self._model.__name__} needs Meta.soft_delete_field"
                )
            return
        field = self.fields_map.get(self.soft_delete_field)
        if field is None:
            raise ConfigurationError(
                f"Meta.soft_delete_field '{self.soft_delete_field}' is not a field on {self._model.__name__}"
            )
        if not isinstance(field, DatetimeField) or not field.null:
            raise ConfigurationError(
                f"Meta.soft_delete_field '{self.soft_delete_field}' on {self._model.__name__} must be a "
                "DatetimeField(null=True) - it also records when the row was deleted"
            )

    def _validate_tenant_field(self) -> None:
        """Checks ``Meta.tenant_field`` and turns a forward FK/O2O relation name into its own
        key column (``company`` -> ``company_id``).

        Raises:
            ConfigurationError: If it names no field, a relation with a composite key, or a
                field without its own column.
        """
        if self.tenant_field is None:
            return
        field = self.fields_map.get(self.tenant_field)
        if field is None:
            raise ConfigurationError(
                f"Meta.tenant_field '{self.tenant_field}' is not a field on {self._model.__name__}"
            )
        if self.tenant_field in self.fk_fields or self.tenant_field in self.o2o_fields:
            relation_field = cast("ForeignKeyFieldInstance[Any]", field)
            if len(relation_field.source_fields) != 1:
                raise ConfigurationError(
                    f"Meta.tenant_field '{self.tenant_field}' on {self._model.__name__} is a relation with a "
                    f"composite key {relation_field.source_fields} - name a single column field instead"
                )
            self.tenant_field = relation_field.source_fields[0]
            return
        if self.tenant_field not in self.fields_db_projection:
            raise ConfigurationError(
                f"Meta.tenant_field '{self.tenant_field}' on {self._model.__name__} has no column of its own - "
                "name a regular field or a forward ForeignKeyField/OneToOneField"
            )

    def _validate_optimistic_lock_field(self) -> None:
        if self.optimistic_lock_field is None:
            return
        field = self.fields_map.get(self.optimistic_lock_field)
        if field is None:
            raise ConfigurationError(
                f"Meta.optimistic_lock_field '{self.optimistic_lock_field}' is not a field on {self._model.__name__}"
            )
        if not isinstance(field, IntField) or field.null:
            raise ConfigurationError(
                f"Meta.optimistic_lock_field '{self.optimistic_lock_field}' on {self._model.__name__} must be a "
                "non-nullable IntField"
            )
        if self.optimistic_lock_field in self.pk_attr_names:
            # The optimistic lock field is bumped on every save(); a primary key column names the
            # row saved - one column can't be both.
            raise ConfigurationError(
                f"Meta.optimistic_lock_field '{self.optimistic_lock_field}' on {self._model.__name__} can't also be "
                "part of the primary key - it needs to be bumped in place on save(), which conflicts "
                "with a primary key column identifying which row is being saved"
            )
        if field.generated:
            raise ConfigurationError(
                f"Meta.optimistic_lock_field '{self.optimistic_lock_field}' on {self._model.__name__} can't be a "
                "DB-generated field - every write bumps it in place"
            )

    def _get_index_expressions(self) -> None:
        for index in self.indexes:
            if isinstance(index, Index):
                index.get_expressions(self._model)

    def _validate_together_field_indexability(self) -> None:
        self._validate_together_entries(self.indexes, ModelOption.INDEXES)
        self._validate_unique_constraint_fields()

    def _validate_together_entries(self, entries: tuple[tuple[str, ...] | Index, ...], meta_attr_name: str) -> None:
        for entry in entries:
            field_names = entry.fields if isinstance(entry, Index) else entry
            # Field.indexable describes a plain btree index - a non-btree access method (e.g. GIN
            # over a JSONField) is exactly how such a column gets indexed.
            is_btree = not (isinstance(entry, Index) and entry.INDEX_TYPE)
            for field_name in field_names:
                field = self.fields_map.get(field_name)
                if field is None:
                    raise ConfigurationError(
                        f"Meta.{meta_attr_name} field '{field_name}' is not a field on {self._model.__name__}"
                    )
                if is_btree and not field.indexable:
                    raise ConfigurationError(
                        f"Meta.{meta_attr_name} field '{field_name}' ({field.__class__.__name__}) on "
                        f"{self._model.__name__} can't be indexed"
                    )

    def _validate_unique_constraint_fields(self) -> None:
        for constraint in self.constraints:
            if not isinstance(constraint, UniqueConstraint):
                continue
            for field_name in constraint.fields:
                field = self.fields_map.get(field_name)
                if field is None:
                    raise ConfigurationError(
                        f"Meta.constraints: UniqueConstraint field '{field_name}' is not a field on "
                        f"{self._model.__name__}"
                    )
                if field_name in self.m2m_fields:
                    raise ConfigurationError(
                        f"Meta.constraints: UniqueConstraint field '{field_name}' on {self._model.__name__} "
                        "is a ManyToManyField, which has no column on this table"
                    )
                if not field.indexable:
                    raise ConfigurationError(
                        f"Meta.constraints: UniqueConstraint field '{field_name}' ({field.__class__.__name__}) "
                        f"on {self._model.__name__} can't be indexed"
                    )

    def _validate_unique_indexes_do_not_collide_with_constraints(self) -> None:
        """Rejects an ``Index(unique=True)`` and a ``UniqueConstraint`` over the same set of fields -
        two physical objects enforcing one rule. The field order doesn't matter.
        """
        unique_constraint_field_sets = {
            frozenset(constraint.fields) for constraint in self.constraints if isinstance(constraint, UniqueConstraint)
        }
        for index in self.indexes:
            if not isinstance(index, Index) or not index.unique or not index.fields:
                continue
            if frozenset(index.fields) in unique_constraint_field_sets:
                raise ConfigurationError(
                    f"Model {self._model.__name__}: Meta.indexes has a unique Index(fields={index.fields!r}) "
                    "that covers the exact same fields as a Meta.constraints UniqueConstraint - both "
                    "independently enforce the same uniqueness rule as two separate physical unique "
                    "indexes, doubling write overhead for no benefit. Remove the redundant declaration."
                )

    def _validate_unnamed_unique_constraints_are_distinct(self) -> None:
        """Two unnamed ``UniqueConstraint``s on the same fields, in the same order, get the same
        generated name - the second fails schema creation with a raw "already exists" error.

        Raises:
            ConfigurationError: Two such constraints are declared.
        """
        unnamed_field_lists: set[tuple[str, ...]] = set()
        for constraint in self.constraints:
            if not isinstance(constraint, UniqueConstraint) or constraint.name:
                continue
            field_names = tuple(constraint.fields)
            if field_names in unnamed_field_lists:
                raise ConfigurationError(
                    f"Model {self._model.__name__}: Meta.constraints declares two unnamed "
                    f"UniqueConstraint(fields={field_names!r}) - they get the same generated name. Remove "
                    "the redundant one, or give one of them a name."
                )
            unnamed_field_lists.add(field_names)

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
            hooks = {*self.fk_fields, *self.o2o_fields, *self.fk_shadow_columns}
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
            from hare.models.instance_constructors import InstanceConstructors

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
            self.m2m_fields | self.backward_fk_fields | self.fk_fields | self.backward_o2o_fields | self.o2o_fields
        )
        # Precomputed once here rather than recomputed on every _snapshot_dirty_fields()/
        # get_dirty_fields()/clone() call - fields_map/fetch_fields are both fixed once fields
        # are finalised, never per-instance.
        self.direct_fields = frozenset(self.fields_map.keys() - self.fetch_fields)
        self.sensitive_fields = frozenset(name for name, field in self.fields_map.items() if field.sensitive)

        generated_fields = [
            (field.source_field or field.model_field_name) for field in self.fields_map.values() if field.generated
        ]
        self.generated_db_fields = tuple(generated_fields)
        self.recomputed_db_fields = tuple(column for column in generated_fields if column != self.db_pk_column)
        # Computed once, not per hydrated row. A composite primary key is never generated.
        self.default_custom_generated_pk = not isinstance(self.pk_attr, tuple) and (
            self.db_pk_column not in self.generated_db_fields
        )

        self.generated_pk_field_name = (
            self.pk_attr if isinstance(self.pk_attr, str) and self.pk is not None and self.pk.generated else None
        )

        db_default_cols = [
            (field.source_field or field.model_field_name)
            for field in self.fields_map.values()
            if field.has_db_default() and not field.generated
        ]
        self.db_default_db_columns = tuple(db_default_cols)

        self._ordering_validated = True
        for field_name, _ in self._default_ordering:
            # "pk" is never a real key in self.fields (only QuerySet.filter()/get() special-case
            # that alias) - resolving it to the real pk_attr name is deferred to the `ordering`
            # property itself (see its own comment), so it must not be flagged unknown here.
            if field_name != "pk" and field_name.partition("__")[0] not in self.fields:
                self._ordering_validated = False
                break

    def _generate_lazy_forward_relation_fields(self, keys: Iterable[str]) -> None:
        """Creates the lazy get/set/del properties of forward FK/O2O fields."""
        # Deferred import: the Model module imports this one.
        from hare.models.model import Model

        for key in keys:
            _key = f"_{key}"
            field_object = cast("ForeignKeyFieldInstance[Any] | OneToOneFieldInstance[Any]", self.fields_map[key])
            relation_fields = field_object.source_fields
            to_fields = tuple(f.model_field_name for f in field_object.to_field_instances)
            for relation_field in relation_fields:
                self.fk_shadow_columns[relation_field] = _key
            property_kwargs: FkSetterKwargs = FkSetterKwargs(
                _key=_key,
                relation_fields=relation_fields,
                to_fields=to_fields,
            )
            setattr(
                self._model,
                key,
                property(
                    partial(
                        Model._fk_getter,
                        ftype=field_object.related_model,
                        **property_kwargs,
                    ),
                    partial(
                        Model._fk_setter,
                        **property_kwargs,
                    ),
                    partial(
                        Model._fk_setter,
                        value=None,
                        **property_kwargs,
                    ),
                ),
            )

    def _generate_lazy_backward_relation_fields(
        self,
        keys: Iterable[str],
        getter: Callable[..., ReverseRelation[Model] | QuerySetSingle[Model | None]],
    ) -> None:
        """Creates the lazy get-only properties of backward FK/O2O fields - ``getter`` resolves a
        ReverseRelation for the former, a single cached instance for the latter.
        """
        for key in keys:
            _key = f"_{key}"
            field_object = cast("BackwardFKRelation[Any] | BackwardOneToOneRelation[Any]", self.fields_map[key])
            setattr(
                self._model,
                key,
                property(
                    partial(
                        getter,
                        _key=_key,
                        ftype=field_object.related_model,
                        frelfields=field_object.relation_fields,
                        from_fields=tuple(f.model_field_name for f in field_object.to_field_instances),
                    )
                ),
            )

    def _generate_lazy_fk_m2m_fields(self) -> None:
        # See _generate_lazy_forward_relation_fields() for why this import is deferred.
        from hare.models.model import Model

        self._generate_lazy_forward_relation_fields(self.fk_fields)
        self._generate_lazy_backward_relation_fields(self.backward_fk_fields, Model._rfk_getter)
        self._generate_lazy_forward_relation_fields(self.o2o_fields)
        self._generate_lazy_backward_relation_fields(self.backward_o2o_fields, Model._ro2o_getter)

        # Create lazy M2M fields on model.
        for key in self.m2m_fields:
            _key = f"_{key}"
            field_object = cast("ManyToManyFieldInstance[Any]", self.fields_map[key])
            setattr(
                self._model,
                key,
                property(partial(Model._m2m_getter, _key=_key, field_object=field_object)),
            )

    def _generate_db_fields(self) -> None:
        HydrationLayout.layouts.forget_model(self._model)

    def get_hydration_layout(self, db: DatabaseClient) -> HydrationLayout:
        """How the model's rows are read on ``db``.

        Args:
            db: The connection the rows are read on.

        Returns:
            The layout.
        """
        layouts: dict[tuple[frozenset[type], Dialect], HydrationLayout] | None = self.hydration_layouts
        if layouts is None:
            layouts = HydrationLayout.layouts.get_model_bucket(self._model)
        key = (db.native_python_types, db.dialect)
        layout = layouts.get(key)
        if layout is None:
            layout = layouts[key] = HydrationLayout.build(self, db)
        return layout

    @staticmethod
    def _raise_if_unsupported_option_declared(meta: type[Model.Meta] | None) -> None:
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
    def _get_together(meta: type[Model.Meta] | None, together: str) -> tuple[tuple[str, ...], ...]:
        _together = getattr(meta, together, ())

        if _together and isinstance(_together, (list, tuple)) and isinstance(_together[0], str):
            _together = (_together,)

        # return without validation, validation will be done further in the code
        return _together

    @staticmethod
    def _prepare_default_ordering(
        meta: type[Model.Meta] | None,
    ) -> tuple[tuple[str, Order], ...]:
        ordering_list = getattr(meta, ModelOption.ORDERING, ())

        return tuple(QuerySet._get_ordering_string(ordering) for ordering in ordering_list)
