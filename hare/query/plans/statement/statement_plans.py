from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar

from hare.core.caching.cache import Cache
from hare.query.plans.statement.statement_plan import StatementPlan

if TYPE_CHECKING:
    from hare.models import Model
    from hare.models.write.declarations import InstanceWriteTemplates
    from hare.query.plans.statement.raw_sql_plan import RawSQLPlan
    from hare.query.rows.model_rows.model_columns import ModelColumns
    from hare.query.rows.native.hydration_layout import HydrationEntry


class StatementPlans:
    """The one owner of what hare keeps to run queries again: the plan of every plan key, the row
    decode plans and prepared write statements. A change to a
    model or to the registries drops what was kept for it, as from every registered cache (``Caches``).
    The stores are bounded per model - the least recently used entries go first - and a collected
    model class takes its entries with it.
    """

    #: Plan key -> the plan kept under it.
    plans: ClassVar[Cache[StatementPlan]] = Cache(Cache.max_size_from_env())
    #: (model, dialect, connection alias, native types, selected columns, joined buckets) -> the positional row
    #: decode plan of a model query selecting those columns, None where rows are read by name.
    decode_plans: ClassVar[Cache[tuple[HydrationEntry, ...] | None]] = Cache(Cache.max_size_from_env())
    #: (model, connection alias, dialect, builder class, schema, table) -> the statements and
    #: column lists ``InstanceWriter`` writes one instance of the model with.
    instance_writes: ClassVar[Cache[InstanceWriteTemplates]] = Cache(Cache.max_size_from_env())
    #: (model, placeholders, first parameter index, row count) -> the ``VALUES`` rows SQL of a bulk
    #: write of the model.
    values_rows_sql: ClassVar[Cache[str]] = Cache(Cache.max_size_from_env())
    #: (model, connection alias, dialect, builder class, schema, table, columns, conflict,
    #: returning) -> the single-row INSERT template of a bulk create of the model and the values
    #: of the parameters following the row's; ``None`` columns for its ``DEFAULT VALUES`` INSERT.
    insert_templates: ClassVar[Cache[tuple[str, list[Any]]]] = Cache(Cache.max_size_from_env())
    #: (model, "calls", query class, ...) -> the plan of ``plans`` a query of a queryset made by
    #: simple calls alone runs on (``CallSignaturePlans``), with the position of the call value each
    #: of its values is (None in the plan's own order) and what the query class reads its result
    #: with - found in one lookup, without hashing the plan's own key; dropped whenever ``plans``
    #: drops any plan, so a plan dropped there is no longer found by its calls either.
    call_signature_plans: ClassVar[Cache[tuple[StatementPlan, tuple[int, ...] | None, Any]]] = Cache(
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
    #: (model, connection alias, dialect, native types, result column names, annotations, read by position,
    #: select_related buckets, compiler decode-plan key, partial selection) -> the columns of each model
    #: the rows of a result hold
    #: (``ModelRows.get_columns()``).
    row_columns: ClassVar[Cache[list[ModelColumns]]] = Cache(Cache.max_size_from_env(), holds_sql=False)
    #: How many queries ran on a found plan - for tests.
    hits: ClassVar[int] = 0

    @classmethod
    def find(cls, model: type[Model], key: tuple[Any, ...]) -> StatementPlan | None:
        """The plan kept under a key.

        Args:
            model: The lifetime holder of the plan.
            key: The plan key.

        Returns:
            The plan, or None when the key has none yet.
        """
        return cls.plans.get_bucket(model).get(key)

    @classmethod
    def record(cls, model: type[Model], key: tuple[Any, ...], plan: StatementPlan) -> None:
        """Keeps the plan a query was built into under its key.

        Args:
            model: The lifetime holder of the plan.
            key: The plan key.
            plan: The plan.
        """
        cls.plans.get_bucket(model)[key] = plan

    @classmethod
    def count_hit(cls) -> None:
        """Counts a query that ran on its plan."""
        cls.hits += 1
