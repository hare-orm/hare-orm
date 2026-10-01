from __future__ import annotations

from collections.abc import Collection, Generator, Iterable, Mapping
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

from hare.core.lookup_path import LookupPath
from hare.fields.relations.fields.many_to_many_field_instance import ManyToManyFieldInstance
from hare.query.expressions.constants import (
    ISNULL_LOOKUP_SUFFIX,
    MULTIPLE_VALUE_TYPES,
    SEPARATE_FILTER_JOIN_PATH_SEPARATOR,
)

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.fields.relations.fields.relational_field import RelationalField
    from hare.models import Model
    from hare.query.expressions.base.expression_context import ExpressionContext
from hare.query.expressions.aggregate_paths.aggregate_crossing import AggregateCrossing


class AggregatedMultiValuedPaths(dict[str, bool]):
    """Tracks the to-many relation JOINs of a query, to detect aggregates that another JOIN fans out.

    As a dict it maps each to-many path (``products``, ``products__reviews``) crossed by an
    aggregate to whether every aggregate crossing it used distinct=True.

    Attributes:
        aggregate_crossings: One entry per resolved aggregate expression.
        row_paths: Paths joined outside any aggregate - by ``.filter()``/``.exclude()``
            conditions, non-aggregate annotations, ``values()`` fields and the ordering. A separate
            JOIN a later ``.filter()`` call builds over an already joined relation is recorded as
            ``<path>#<generation>``, a path of its own.
        group_key_paths: Paths walked by a GROUP BY key - each group holds one related row's key
            value, so these JOINs partition the rows instead of repeating them.
        unique_group_key_paths: Paths whose related row the GROUP BY keys identify - they read the
            relation's primary key, a non-nullable unique field or every field of a non-nullable
            unique set, so each group holds exactly one row of it.
        group_key_field_names_by_path: Per to-many path, the fields of the related model the GROUP
            BY keys read.
        non_null_field_names_by_path: Per to-many path, the fields of the related model a filter of
            the query keeps from being NULL on the path's JOIN.
        pinned_paths: Tracked paths whose JOIN a filter narrows to at most one related row per row
            it is joined to - they repeat no row.
        is_recording_row_joins: Whether a path recorded outside any aggregate is a JOIN of the query.
    """

    def __init__(self) -> None:
        super().__init__()
        self.aggregate_crossings: list[AggregateCrossing] = []
        self.row_paths: list[str] = []
        self.group_key_paths: set[str] = set()
        self.unique_group_key_paths: set[str] = set()
        self.group_key_field_names_by_path: dict[str, set[str]] = {}
        self.non_null_field_names_by_path: dict[str, set[str]] = {}
        self.pinned_paths: set[str] = set()
        self.is_recording_row_joins = False
        self._active_crossing: AggregateCrossing | None = None
        self._is_in_aggregate_filter = False

    @classmethod
    def get_from(cls, expression_context: ExpressionContext) -> AggregatedMultiValuedPaths | None:
        """Returns the tracker carried by a resolve context, if any.

        Args:
            expression_context: The context being resolved.

        Returns:
            The tracker, or None when the context does not track aggregated relations.
        """
        paths = expression_context.aggregated_multi_valued_paths
        return paths if isinstance(paths, cls) else None

    @staticmethod
    def get_multi_valued_paths(model: type[Model], lookup: str, prefix: str = "") -> list[str]:
        """Lists every to-many relation path a ``a__b__c`` lookup walks through.

        Args:
            model: The model the lookup starts from.
            lookup: The double-underscore lookup, e.g. ``products__reviews__stars``.
            prefix: The relation path already walked to reach ``model``.

        Returns:
            The cumulative path of each to-many hop, e.g. ``["products", "products__reviews"]``.
        """
        paths: list[str] = []
        current_model = model
        current_path = prefix
        for part in lookup.split("__"):
            relation = current_model._meta.fields_map.get(part)
            related_model = getattr(relation, "related_model", None)
            if relation is None or related_model is None:
                break
            current_path = f"{current_path}__{part}" if current_path else part
            if getattr(relation, "is_multi_valued", False):
                paths.append(current_path)
            current_model = related_model
        return paths

    @contextmanager
    def aggregate_scope(self, distinct: bool, aggregate_name: str = "") -> Generator[AggregateCrossing]:
        """Attributes every path recorded inside the block to one aggregate expression.

        Args:
            distinct: Whether the aggregate was declared with distinct=True, or ignores repeated rows.
            aggregate_name: The aggregate's class name.
        """
        crossing = AggregateCrossing(distinct=distinct, aggregate_name=aggregate_name)
        previous_state = self._active_crossing, self._is_in_aggregate_filter
        self._active_crossing, self._is_in_aggregate_filter = crossing, False
        try:
            yield crossing
        finally:
            self._active_crossing, self._is_in_aggregate_filter = previous_state
        # An aggregate nested in another one's argument (Max(Count("products"))) crosses its
        # paths on behalf of the enclosing aggregate too.
        if self._active_crossing is not None:
            for path in (*crossing.argument_paths, *crossing.filter_paths):
                self.record_path(path)
        self.aggregate_crossings.append(crossing)
        for path in {*crossing.argument_paths, *crossing.filter_paths}:
            self[path] = self.get(path, True) and distinct

    @contextmanager
    def aggregate_filter_scope(self) -> Generator[None]:
        """Attributes paths recorded inside the block to the active aggregate's ``_filter``."""
        previous_state = self._is_in_aggregate_filter
        self._is_in_aggregate_filter = True
        try:
            yield
        finally:
            self._is_in_aggregate_filter = previous_state

    def record_path(self, path: str, filter_call_generation: int = 0) -> None:
        """Records a to-many JOIN made by an aggregate's expression, or by the query outside any
        aggregate.

        Args:
            path: The cumulative to-many relation path, e.g. ``products__reviews``.
            filter_call_generation: Non-zero when a later ``.filter()`` call builds its own,
                separate JOIN over the relation.
        """
        if filter_call_generation:
            path = f"{path}{SEPARATE_FILTER_JOIN_PATH_SEPARATOR}{filter_call_generation}"
        crossing = self._active_crossing
        if crossing is not None:
            (crossing.filter_paths if self._is_in_aggregate_filter else crossing.argument_paths).append(path)
        elif self.is_recording_row_joins:
            self.row_paths.append(path)

    def record_lookup(self, model: type[Model], lookup: str, prefix: str = "") -> None:
        """Records the to-many hops of a lookup.

        Args:
            model: The model the lookup starts from.
            lookup: The double-underscore lookup.
            prefix: The relation path already walked to reach ``model``.
        """
        for path in self.get_multi_valued_paths(model, lookup, prefix):
            self.record_path(path)

    def get_mark(self) -> int:
        """Returns a position that ``discard_since()`` can later roll recorded paths back to."""
        return len(self._get_target_paths())

    def discard_since(self, mark: int) -> None:
        """Forgets the paths recorded since ``mark`` - their JOINs went into a subquery.

        Args:
            mark: A value ``get_mark()`` returned.
        """
        del self._get_target_paths()[mark:]

    def _get_target_paths(self) -> list[str]:
        crossing = self._active_crossing
        if crossing is None:
            return self.row_paths
        return crossing.filter_paths if self._is_in_aggregate_filter else crossing.argument_paths

    @staticmethod
    def is_single_chain(paths: set[str]) -> bool:
        """Whether every path is the parent of, or the same as, the deepest one.

        Args:
            paths: Cumulative to-many relation paths.

        Returns:
            True when the paths all lie on one root-to-leaf relation chain.
        """
        if not paths:
            return True
        deepest_path = max(paths, key=len)
        return all(deepest_path == path or deepest_path.startswith(f"{path}__") for path in paths)

    def record_group_key_lookup(self, model: type[Model], lookup: str) -> None:
        """Records the to-many hops of a GROUP BY key.

        Args:
            model: The model the lookup starts from.
            lookup: The double-underscore lookup.
        """
        multi_valued_paths = self.get_multi_valued_paths(model, lookup)
        self.group_key_paths.update(multi_valued_paths)
        if not multi_valued_paths:
            return
        key = self.get_last_multi_valued_key(model, lookup)
        if key is None:
            return
        related_model, field_names = key
        last_path = multi_valued_paths[-1]
        key_field_names = self.group_key_field_names_by_path.setdefault(last_path, set())
        key_field_names.update(field_names)
        if self.field_names_identify_row(
            related_model, key_field_names, self.non_null_field_names_by_path.get(last_path, ())
        ):
            self.unique_group_key_paths.add(last_path)

    def record_non_null_lookup(self, model: type[Model], lookup: str) -> None:
        """Records a field a filter keeps from being NULL on its to-many relation's JOIN - recorded
        before the GROUP BY keys, whose uniqueness it can complete.

        Args:
            model: The model the lookup starts from.
            lookup: The double-underscore lookup of the field, without a lookup suffix.
        """
        multi_valued_paths = self.get_multi_valued_paths(model, lookup)
        key = self.get_last_multi_valued_key(model, lookup) if multi_valued_paths else None
        if key is None:
            return
        _related_model, field_names = key
        self.non_null_field_names_by_path.setdefault(multi_valued_paths[-1], set()).update(field_names)

    def record_row_pinning_conditions(
        self,
        model: type[Model],
        conditions_by_generation: Mapping[int, Iterable[tuple[str, Any]]],
        claiming_generation_by_path: Mapping[str, int],
    ) -> None:
        """Records the to-many JOINs a filter narrows to at most one related row per joined row - an
        equality on the related primary key, a unique field or every field of a unique set, or
        ``__isnull=True`` on a column a reverse relation's row can't hold NULL in. Such a JOIN
        repeats no row and inflates no aggregate.

        Args:
            model: The queried model.
            conditions_by_generation: Per ``.filter()`` call generation, the kwargs every kept row
                satisfies.
            claiming_generation_by_path: Per to-many path, the generation whose filters read the
                path's own JOIN.
        """
        equal_field_names_by_tracked_path: dict[str, set[str]] = {}
        related_model_by_tracked_path: dict[str, type[Model]] = {}
        for generation, conditions in conditions_by_generation.items():
            for key, value in conditions:
                lookup = key
                relation_lookup, separator, suffix = key.rpartition("__")
                is_isnull = bool(separator) and suffix == ISNULL_LOOKUP_SUFFIX
                if is_isnull:
                    if value is not True:
                        continue
                    lookup = relation_lookup
                elif not self.is_single_value(value):
                    continue
                multi_valued_paths = self.get_multi_valued_paths(model, lookup)
                last_multi_valued_key = self.get_last_multi_valued_key(model, lookup) if multi_valued_paths else None
                if last_multi_valued_key is None:
                    continue
                related_model, field_names = last_multi_valued_key
                path = multi_valued_paths[-1]
                tracked_path = (
                    path
                    if not generation or claiming_generation_by_path.get(path) == generation
                    else f"{path}{SEPARATE_FILTER_JOIN_PATH_SEPARATOR}{generation}"
                )
                relation = self.get_last_relation(model, path)
                if is_isnull:
                    # A many-to-many JOIN can find link rows whose related row its ON clause
                    # rejects - one NULL row each.
                    if not isinstance(relation, ManyToManyFieldInstance) and all(
                        self._is_read_non_nullable_field(related_model, field_name, set(field_names), set())
                        for field_name in field_names
                    ):
                        self.pinned_paths.add(tracked_path)
                    continue
                if isinstance(relation, ManyToManyFieldInstance) and not self.links_are_unique(relation):
                    continue
                related_model_by_tracked_path[tracked_path] = related_model
                equal_field_names_by_tracked_path.setdefault(tracked_path, set()).update(field_names)
        for tracked_path, equal_field_names in equal_field_names_by_tracked_path.items():
            # An equality never matches NULL, so a nullable unique field identifies a row too.
            if self.field_names_identify_row(
                related_model_by_tracked_path[tracked_path], equal_field_names, equal_field_names
            ):
                self.pinned_paths.add(tracked_path)

    @staticmethod
    def is_single_value(value: Any) -> bool:
        """Whether an equality filter's value is one value - not NULL, a collection or a query.

        Args:
            value: The filter value.

        Returns:
            True for one value.
        """
        if value is None or isinstance(value, MULTIPLE_VALUE_TYPES):
            return False
        # A queryset compares as a set of rows.
        return not hasattr(value, "_make_query")

    @staticmethod
    def get_last_relation(model: type[Model], path: str) -> RelationalField[Model] | None:
        """The relation a ``relation__relation`` path ends on.

        Args:
            model: The model the path starts from.
            path: The relation path.

        Returns:
            The relation, or None when a part isn't a relation.
        """
        lookup_path = LookupPath.parse(model, path, crosses_last=True)
        if lookup_path.rest or not lookup_path.relations:
            return None
        return lookup_path.relations[-1]

    @classmethod
    def links_are_unique(cls, relation: ManyToManyFieldInstance[Any]) -> bool:
        """Whether a many-to-many relation links two rows at most once - its automatic through
        table's unique index, or a unique set of the through model covering both key columns.

        Args:
            relation: The relation.

        Returns:
            True when no two link rows join the same pair of rows.
        """
        through_model = relation.through_model_class
        if through_model is None:
            return bool(relation.unique)
        field_names_by_column = through_model._meta.fields_db_projection_reverse
        key_field_names = [
            field_names_by_column.get(column, column) for column in (*relation.backward_keys, *relation.forward_keys)
        ]
        return cls.field_names_identify_row(through_model, key_field_names)

    @staticmethod
    def get_last_multi_valued_key(model: type[Model], lookup: str) -> tuple[type[Model], tuple[str, ...]] | None:
        """The model a lookup's last to-many hop reaches, and the fields of it the lookup reads -
        ``tags`` and ``tags__pk`` read the primary key, ``tags__slug`` the ``slug`` field.

        Args:
            model: The model the lookup starts from.
            lookup: The double-underscore lookup.

        Returns:
            The model and the field names, or None when the lookup doesn't end on a column of the
            model its last to-many hop reaches.
        """
        current_model = model
        is_on_last_multi_valued_model = False
        parts = lookup.split("__")
        for index, part in enumerate(parts):
            relation = current_model._meta.fields_map.get(part)
            related_model = getattr(relation, "related_model", None)
            if relation is None or related_model is None:
                if not is_on_last_multi_valued_model or index != len(parts) - 1:
                    return None
                if part == "pk":
                    return current_model, tuple(current_model._meta.pk_attr_names)
                return (current_model, (part,)) if relation is not None else None
            if (
                is_on_last_multi_valued_model
                and index == len(parts) - 1
                and not getattr(relation, "is_multi_valued", False)
                and getattr(relation, "source_fields", None)
            ):
                # A forward relation of the reached model reads its own key columns.
                return current_model, (part,)
            is_on_last_multi_valued_model = bool(getattr(relation, "is_multi_valued", False))
            current_model = related_model
        if not is_on_last_multi_valued_model:
            return None
        # The lookup ends on the relation itself, which reads the related primary key.
        return current_model, tuple(current_model._meta.pk_attr_names)

    @classmethod
    def field_names_identify_row(
        cls, model: type[Model], field_names: Collection[str], non_null_field_names: Collection[str] = ()
    ) -> bool:
        """Whether no two rows of a model can share the values of some fields - they include the
        primary key, a non-nullable unique field, or every field of an
        unconditional ``UniqueConstraint`` whose columns are all non-nullable.

        Args:
            model: The model.
            field_names: The field names.
            non_null_field_names: Nullable fields known to hold no NULL on the rows compared.

        Returns:
            True when the fields identify one row.
        """
        from hare.ddl.constraints.unique_constraint import UniqueConstraint

        meta = model._meta
        read_field_names = set(field_names)
        known_non_null_field_names = set(non_null_field_names)
        if "pk" in read_field_names or set(meta.pk_attr_names) <= read_field_names:
            return True
        unique_field_sets: list[tuple[str, ...]] = [
            (field_name,) for field_name, field in meta.fields_map.items() if field.unique and not field.pk
        ]
        unique_field_sets.extend(
            tuple(constraint.fields)
            for constraint in meta.constraints
            if isinstance(constraint, UniqueConstraint) and not constraint.condition and constraint.fields
        )
        return any(
            all(
                cls._is_read_non_nullable_field(model, field_name, read_field_names, known_non_null_field_names)
                for field_name in unique_field_set
            )
            for unique_field_set in unique_field_sets
        )

    @staticmethod
    def _is_read_non_nullable_field(
        model: type[Model], field_name: str, read_field_names: set[str], non_null_field_names: set[str]
    ) -> bool:
        """Whether a field is read and can't hold NULL - a forward relation through its own name or
        all of its key columns.

        Args:
            model: The model.
            field_name: The field name.
            read_field_names: The field names read.
            non_null_field_names: Nullable fields known to hold no NULL.

        Returns:
            True for a read, non-nullable field.
        """
        fields_map = model._meta.fields_map
        field = fields_map.get(field_name)
        if field is None:
            return False
        source_field_names: tuple[str, ...] = tuple(getattr(field, "source_fields", None) or ())
        if not source_field_names:
            return field_name in read_field_names and (not field.null or field_name in non_null_field_names)
        is_read = field_name in read_field_names or all(name in read_field_names for name in source_field_names)
        return is_read and all(
            (source_field := fields_map.get(name)) is not None
            and (not source_field.null or name in non_null_field_names or field_name in non_null_field_names)
            for name in source_field_names
        )

    @classmethod
    def lookup_ends_on_unique_field(cls, model: type[Model], lookup: str) -> bool:
        """Whether a lookup reads the primary key or a non-nullable unique field of the model its
        last to-many hop reaches - ``tags``, ``tags__id``, ``tags__slug`` with ``slug`` unique and
        not nullable.

        Args:
            model: The model the lookup starts from.
            lookup: The double-underscore lookup.

        Returns:
            True when the lookup's value identifies one row of its last to-many relation.
        """
        key = cls.get_last_multi_valued_key(model, lookup)
        return key is not None and cls.field_names_identify_row(*key)

    def get_partitioning_paths(self) -> set[str]:
        """The to-many paths whose JOIN a GROUP BY key splits into groups instead of repeating rows
        within one. A key identifying one related row always partitions its relation, once every
        to-many relation above it is partitioned; any other key (``tags__name``) only while no
        non-distinct aggregate reads another to-many relation.

        Returns:
            The partitioning paths.
        """
        if not any(
            not crossing.distinct and not set(crossing.argument_paths) - self.pinned_paths <= self.group_key_paths
            for crossing in self.aggregate_crossings
        ):
            return set(self.group_key_paths)
        return {
            path
            for path in self.unique_group_key_paths
            if all(
                parent_path in self.unique_group_key_paths
                for parent_path in self.group_key_paths
                if path.startswith(f"{parent_path}__")
            )
        }

    @staticmethod
    def get_path_display_name(path: str) -> str:
        """Returns a tracked path as the error text names it.

        Args:
            path: A tracked path, possibly carrying a separate filter JOIN's generation.

        Returns:
            The relation path, marked when it is a separate ``.filter()`` call's own JOIN.
        """
        relation_path, separator, _generation = path.partition(SEPARATE_FILTER_JOIN_PATH_SEPARATOR)
        if not separator:
            return relation_path
        return f"{relation_path} (the separate JOIN of another .filter() call)"

    def get_fan_out_error_message(self) -> str | None:
        """Finds a non-distinct aggregate another to-many JOIN of the query would inflate. An aggregate
        is correct when its relation chain is the query's only to-many JOIN or when it is distinct;
        an aggregate over the base table's own columns is inflated by any to-many JOIN. A JOIN
        partitioning the groups doesn't count.

        Returns:
            The error text for the first inflated aggregate, None when there is none.
        """
        all_paths = set(self.row_paths)
        for crossing in self.aggregate_crossings:
            all_paths.update(crossing.argument_paths, crossing.filter_paths)
        # A JOIN a filter narrows to one related row repeats no row either.
        partitioning_paths = self.get_partitioning_paths() | self.pinned_paths
        all_paths -= partitioning_paths
        if not all_paths:
            return None
        for crossing in self.aggregate_crossings:
            argument_paths = set(crossing.argument_paths) - partitioning_paths
            if crossing.distinct:
                continue
            if argument_paths == all_paths and self.is_single_chain(argument_paths):
                continue
            joined_relations = ", ".join(sorted(self.get_path_display_name(path) for path in all_paths))
            aggregated_rows = ", ".join(sorted(argument_paths)) or "the base table rows"
            joined_relation_paths = {path.partition(SEPARATE_FILTER_JOIN_PATH_SEPARATOR)[0] for path in all_paths}
            if len(joined_relation_paths) == 1:
                joins_description = "a JOIN" if len(all_paths) == 1 else "several JOINs"
                problem = (
                    f"A non-distinct aggregate (Count/Sum/Avg/...) over {aggregated_rows} is combined "
                    f"with {joins_description} over the to-many relation {joined_relations}"
                )
            else:
                problem = (
                    "Combining aggregates (Count/Sum/Avg/...) over more than one to-many relation "
                    f"({joined_relations}) in the same query"
                )
            if crossing.aggregate_name == "Count":
                remedy = (
                    "Pass distinct=True to Count() of the relation itself, move the aggregate into a "
                    "Subquery() annotation, or split the query."
                )
            else:
                remedy = (
                    f"distinct=True doesn't help {crossing.aggregate_name or 'this aggregate'}() - it would "
                    "drop equal values, not repeated rows. Compute it in a Subquery() annotation over the "
                    "related model instead, or split the query."
                )
            non_unique_group_key_paths = sorted(
                (self.group_key_paths - partitioning_paths) & all_paths, key=lambda path: (len(path), path)
            )
            if non_unique_group_key_paths:
                group_key_relations = ", ".join(non_unique_group_key_paths)
                group_key_note = (
                    f" The GROUP BY key over {group_key_relations} is neither the relation's primary key nor a "
                    "unique field (a non-nullable one, or every field of a non-nullable unique set), so it "
                    "doesn't pin one related row per group - two related rows can share "
                    "a key value, and each repeats the rows of the other JOINs. Add the relation's primary "
                    f"key to the key (e.g. values('{non_unique_group_key_paths[0]}__<pk field>', ...)) - a key "
                    "identifying one related row splits the rows exactly."
                )
                if all_paths - set(non_unique_group_key_paths) == argument_paths and self.is_single_chain(
                    argument_paths
                ):
                    return (
                        f"A non-distinct aggregate (Count/Sum/Avg/...) over {aggregated_rows} grouped by a "
                        f"values()/group_by() key over the to-many relation {group_key_relations} produces an "
                        f"ambiguous result.{group_key_note} Alternatively: {remedy}"
                    )
            else:
                group_key_note = ""
            return (
                f"{problem} - through several aggregates, an aggregate's _filter=, a .filter() call, "
                "a non-aggregate annotation, a values() field or the ordering - produces an ambiguous "
                "result: each to-many JOIN repeats the rows of the others before any aggregate runs, "
                f"inflating a non-distinct aggregate over {aggregated_rows} by the other relations' row "
                f"counts.{group_key_note} {remedy}"
            )
        return None
