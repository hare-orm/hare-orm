from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar

from hare.core.caching.cache import Cache
from hare.query.plans.statement.statement_plan import StatementPlan

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.models import Model
    from hare.query.plans.statement.raw_sql_plan import RawSQLPlan
    from hare.query.rows.model_rows.model_columns import ModelColumns
    from hare.query.rows.native.hydration_layout import HydrationEntry


class StatementPlans:
    """The one owner of what hare keeps to run queries again: the plan of every plan key, the row
    decode plans, the compiled row readers and the accelerator's read and write plans. A change to a
    model or to the registries drops what was kept for it (``forget_model()``/``forget_all()``). The
    stores are bounded per model - the least recently used entries go first - and a collected model
    class takes its entries with it.
    """

    #: Plan key -> the plan kept under it.
    plans: ClassVar[Cache[StatementPlan]] = Cache(Cache.max_size_from_env())
    #: (model, dialect, connection alias, selected columns, joined buckets) -> the positional row
    #: decode plan of a model query selecting those columns, None where rows are read by name.
    decode_plans: ClassVar[Cache[tuple[HydrationEntry, ...] | None]] = Cache(Cache.max_size_from_env())
    #: (model, connection alias, dialect, builder class, schema, table) -> the statements and
    #: column lists ``InstanceWriter`` writes one instance of the model with.
    instance_writes: ClassVar[Cache[tuple[Any, ...]]] = Cache(Cache.max_size_from_env())
    #: (model, columns, column keys, is partial, is related) -> the compiled function building
    #: an instance of the model from a row.
    hydrate_functions: ClassVar[Cache[Any]] = Cache(Cache.max_size_from_env())
    #: (model, hydration entries, is partial, type registry, zone name) -> the
    #: ``rust.native.rows.ModelReader`` reading those columns of the model.
    model_readers: ClassVar[Cache[Any]] = Cache(Cache.max_size_from_env())
    #: (model, columns, type registry, zone name) -> the ``rust.native.rows.ModelWriter``
    #: writing those columns of the model.
    model_writers: ClassVar[Cache[Any]] = Cache(Cache.max_size_from_env())
    #: (model, value fields, type registry, native types, zone name) -> the
    #: ``rust.native.rows.ValuesReader`` reading those columns of a values query of the model.
    values_readers: ClassVar[Cache[Any]] = Cache(Cache.max_size_from_env())
    #: (model, placeholders, first parameter index, row count) -> the ``VALUES`` rows SQL of a bulk
    #: write of the model.
    values_rows_sql: ClassVar[Cache[str]] = Cache(Cache.max_size_from_env())
    #: (model, connection alias, dialect, builder class, schema, table, columns, conflict,
    #: returning) -> the single-row INSERT template of a bulk create of the model and the values
    #: of the parameters following the row's; ``None`` columns for its ``DEFAULT VALUES`` INSERT.
    insert_templates: ClassVar[Cache[tuple[str, list[Any]]]] = Cache(Cache.max_size_from_env())
    #: (field's model, field, type registry) -> the native writer of a list of the field's filter
    #: values and the value type it converts (``HydrateAccelerator.get_lookup_list_writer()``),
    #: False for a field without one.
    lookup_list_writers: ClassVar[Cache[Any]] = Cache(Cache.max_size_from_env())
    #: (field's model, field, type registry) -> the native writer of one filter value of the field
    #: and the value type it converts (``HydrateAccelerator.get_lookup_writer()``), False for a
    #: field without one.
    lookup_writers: ClassVar[Cache[Any]] = Cache(Cache.max_size_from_env())
    #: (model, "calls", query class, ...) -> the plan of ``plans`` a query of a queryset made by
    #: simple calls alone runs on (``CallSignaturePlans``), with the position of the call value each
    #: of its values is (None in the plan's own order) - found in one lookup, without hashing the
    #: plan's own key; dropped whenever ``plans`` drops any plan, so a plan dropped there is no
    #: longer found by its calls either.
    call_signature_plans: ClassVar[Cache[tuple[StatementPlan, tuple[int, ...] | None]]] = Cache(
        Cache.max_size_from_env(), dropped_with=plans
    )
    #: (model, the writer's key in ``instance_writes``, only a live row, tenant guard applied,
    #: tenant value count, fields) -> the UPDATE ``InstanceWriter`` saves those fields of one
    #: instance with.
    instance_updates: ClassVar[Cache[str]] = Cache(Cache.max_size_from_env())
    #: (model, the writer's key in ``instance_writes``, tenant value count) -> the DELETE of one
    #: row guarded by a tenant scope of that many values.
    instance_deletes: ClassVar[Cache[str]] = Cache(Cache.max_size_from_env())
    #: (field's model, field name, query class) -> the single-row INSERT of a many-to-many
    #: relation's ``add()`` of one instance.
    many_to_many_add_statements: ClassVar[Cache[str]] = Cache(Cache.max_size_from_env())
    #: (model, connection alias, query class, SQL text, parameter types) -> the statement of a
    #: ``.raw()`` query of that text.
    raw_sql_plans: ClassVar[Cache[RawSQLPlan]] = Cache(Cache.max_size_from_env())
    #: (model, connection alias, dialect, column names) -> the columns of a ``.raw()`` query's
    #: rows that are no field of the model - each an attribute of the instances read.
    raw_sql_annotation_names: ClassVar[Cache[list[str]]] = Cache(Cache.max_size_from_env(), holds_sql=False)
    #: (model, connection alias, dialect, result column names, annotations, read by position,
    #: select_related buckets) -> the columns of each model the rows of a result hold
    #: (``ModelRows.get_columns()``).
    row_columns: ClassVar[Cache[list[ModelColumns]]] = Cache(Cache.max_size_from_env(), holds_sql=False)
    #: How many queries ran on a found plan - for tests.
    hits: ClassVar[int] = 0

    @classmethod
    def find(cls, key: tuple[Any, ...]) -> StatementPlan | None:
        """The plan kept under a key.

        Args:
            key: The plan key.

        Returns:
            The plan, or None when the key has none yet.
        """
        return cls.plans.get(key)

    @classmethod
    def find_for_model(cls, model: type[Model], key: tuple[Any, ...]) -> StatementPlan | None:
        """The plan kept under a key that starts with its model - found in the model's own plans,
        with nothing to locate in the key.

        Args:
            model: The model the key starts with.
            key: The rest of the key.

        Returns:
            The plan, or None when the key has none yet.
        """
        return cls.plans.get_for_model(model, key)

    @classmethod
    def record(cls, key: tuple[Any, ...], plan: StatementPlan) -> None:
        """Keeps the plan a query was built into under its key.

        Args:
            key: The plan key.
            plan: The plan.
        """
        cls.plans[key] = plan

    @classmethod
    def count_hit(cls) -> None:
        """Counts a query that ran on its plan."""
        cls.hits += 1

    @classmethod
    def forget_model(cls, model: type[Model]) -> None:
        """Drops every plan and decode plan kept for ``model``.

        Args:
            model: The model.
        """
        cls.plans.forget_model(model)
        cls.call_signature_plans.forget_model(model)
        cls.lookup_list_writers.forget_model(model)
        cls.decode_plans.forget_model(model)
        cls.instance_writes.forget_model(model)
        cls.instance_updates.forget_model(model)
        cls.instance_deletes.forget_model(model)
        cls.hydrate_functions.forget_model(model)
        cls.model_readers.forget_model(model)
        cls.model_writers.forget_model(model)
        cls.values_readers.forget_model(model)
        cls.values_rows_sql.forget_model(model)
        cls.insert_templates.forget_model(model)
        cls.many_to_many_add_statements.forget_model(model)
        cls.raw_sql_plans.forget_model(model)
        cls.raw_sql_annotation_names.forget_model(model)
        cls.row_columns.forget_model(model)

    @classmethod
    def forget_all(cls) -> None:
        """Drops every plan and decode plan, and the counter."""
        cls.plans.clear()
        cls.call_signature_plans.clear()
        cls.lookup_list_writers.clear()
        cls.decode_plans.clear()
        cls.instance_writes.clear()
        cls.instance_updates.clear()
        cls.instance_deletes.clear()
        cls.hydrate_functions.clear()
        cls.model_readers.clear()
        cls.model_writers.clear()
        cls.values_readers.clear()
        cls.values_rows_sql.clear()
        cls.insert_templates.clear()
        cls.many_to_many_add_statements.clear()
        cls.raw_sql_plans.clear()
        cls.raw_sql_annotation_names.clear()
        cls.row_columns.clear()
        cls.hits = 0
