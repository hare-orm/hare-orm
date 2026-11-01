from __future__ import annotations

from collections import defaultdict
from typing import TYPE_CHECKING, Any, cast

from hare.exceptions import FieldError
from hare.fields.relations.fields.backward_foreign_key_relation import BackwardForeignKeyRelation
from hare.fields.relations.fields.declarations import BackwardOneToOneRelation
from hare.fields.relations.fields.foreign_key_field_instance import ForeignKeyFieldInstance
from hare.fields.relations.fields.many_to_many_field_instance import ManyToManyFieldInstance
from hare.fields.relations.fields.one_to_one_field_instance import OneToOneFieldInstance
from hare.fields.relations.fields.relational_field import RelationalField
from hare.models.class_building.constants import LOOKUP_SEPARATOR
from hare.query.lookup_info.lookup_path import LookupPath
from hare.query.lookup_info.lookup_paths import LookupPaths
from hare.query.statements.building.query_joins import QueryJoins

if TYPE_CHECKING:
    from collections.abc import Iterable

    from hare.models import Model
    from hare.query.statements.select.model_rows_query import ModelRowsQuery


class OnlyDeferFields:
    """The columns of the base model a query loads: the .only() whitelist or .defer()'s blacklist
    expanded into one, with the key columns select_related() and prefetch_related() need on each
    instance selected whatever .only() says."""

    @staticmethod
    def get_effective_fields_for_select(query: ModelRowsQuery[Any]) -> tuple[str, ...]:
        """The ``.only()`` whitelist, or ``.defer()``'s blacklist expanded into one.

        Args:
            query: The model rows query.

        Returns:
            The field expressions to select, empty when neither restriction is set.
        """
        if query._deferred_fields:
            return OnlyDeferFields.get_deferred_fields(query)
        return query._fields_for_select

    @staticmethod
    def get_deferred_fields(query: ModelRowsQuery[Any]) -> tuple[str, ...]:
        """Expands the ``.defer()`` fields into the expression list ``.only()`` takes, with every field
        of each ``.select_related()`` path added - ``.defer()`` prunes only the base model's
        columns.

        Args:
            query: The model rows query.

        Returns:
            The expanded field expressions, in model field order.
        """
        fetch_fields = query.model._meta.fetch_fields
        deferred_fields = set(query._deferred_fields)
        expressions = [
            field_name
            for field_name in query.model._meta.fields_map
            if field_name not in fetch_fields and field_name not in deferred_fields
        ]
        expressions.extend(sorted(OnlyDeferFields.prefetch_map_required_local_fields(query) - set(expressions)))

        for relation_path in sorted(query._select_related):
            lookup_path = LookupPath.parse(query.model, relation_path, crosses_last=True)
            for position, related_field in enumerate(lookup_path.relations):
                prefix = "__".join(lookup_path.relation_names[: position + 1])
                related_model = related_field.related_model
                expressions.extend(
                    f"{prefix}__{field_name}"
                    for field_name in related_model._meta.fields_map
                    if field_name not in related_model._meta.fetch_fields
                )

        return tuple(expressions)

    @staticmethod
    def get_only(query: ModelRowsQuery[Any], only_lookup_expressions: tuple[str, ...]) -> None:
        """Selects only the fields ``only()`` names, of the model and of the relations it loads.

        Args:
            query: The query.
            only_lookup_expressions: The fields - ``name``, ``author__name``.

        Raises:
            FieldError: A name is of no field.
        """
        # Group fields by fetch fields, e.g. ["a__b", "a__c"] -> {"a": ["b", "c"]}.
        # The direct fields of the model are the ones that would have the key "".
        fetch_to_fields = defaultdict(list)
        # Shallowest paths first: _select_related_positions gets the entries really selected before the
        # fillers that only tell an empty instance is made.
        for expression in sorted(only_lookup_expressions, key=lambda expression: expression.count("__")):
            fetch_fields_lookup, __, field_name = expression.rpartition("__")
            fetch_to_fields[fetch_fields_lookup].append(field_name)

        # select direct model fields which would have the key "": {"": ["a", "b"]}
        data_fields = fetch_to_fields.pop("", None)
        if data_fields:
            table = query.model._meta.basetable

            # Annotation names in .only() select no model column - the bucket size counts real
            # columns only. An .alias() named like a field means the field.
            def is_real_field(field: str) -> bool:
                return field not in query._annotations or (
                    field in query._alias_keys and field in query.model._meta.fields_db_projection
                )

            real_field_count = sum(1 for field in data_fields if is_real_field(field))
            query._select_related_positions.append(
                (
                    query.model,
                    real_field_count,
                    table,
                    query.model,
                    (None,),
                )
            )
            try:
                query.query = query.query.select(
                    *[
                        table[query.model._meta.fields_db_projection[field]].as_(field)
                        for field in data_fields
                        if is_real_field(field)
                    ]
                )
            except KeyError as error:
                raise FieldError(f"Unknown field {error.args[0]!r} for model {query.model.__name__!r}") from error

        else:
            # No data field is selected - each row still makes an instance of the model, with no
            # field read.
            query._select_related_positions.append(
                (
                    query.model,
                    0,
                    query.model._meta.basetable,
                    query.model,
                    (None,),
                )
            )

        # Select fields of related models, e.g. {"a": ["b", "c"]}
        added_paths = set()
        for fetch_fields_lookup, data_fields in fetch_to_fields.items():
            fetch_fields = LookupPaths.expand_expression(query.model, fetch_fields_lookup)
            extra_conditions = query._select_related_extra_conditions
            model: type[Model] = query.model
            referring_model = model
            table = query.model._meta.basetable
            path: tuple[str | None, ...] = (None,)
            for position, fetch_field in enumerate(fetch_fields):
                if not isinstance(fetch_field, RelationalField):
                    # A path goes on only through a relation - ``only("title__x")`` names no field.
                    raise FieldError(
                        f'Unknown field "{fetch_fields_lookup}__..." for model {query.model.__name__!r} - '
                        f"{fetch_field.model_field_name} is no relation"
                    )
                field = cast("RelationalField[Model]", fetch_field)
                path = path + (field.model_field_name,)
                is_last_field = position == len(fetch_fields) - 1
                # Each relation's extra_condition goes into its JOIN here too - an intermediate one's as
                # well: this JOIN is built before select_related's, and the first JOIN is the one kept.
                table = QueryJoins.join_table_by_field(
                    query,
                    table,
                    field.model_field_name,
                    field,
                    extra_conditions.get(LOOKUP_SEPARATOR.join(cast("tuple[str, ...]", path[1:]))),
                )
                referring_model = model
                model = field.related_model

                if path in added_paths:
                    continue

                # Every hop selects its related model's primary key: only the key tells a joined row
                # from a LEFT JOIN miss.
                hop_field_names = list(
                    dict.fromkeys(
                        (*OnlyDeferFields.get_pk_select_field_names(model), *(data_fields if is_last_field else ()))
                    )
                )
                query._select_related_positions.append((model, len(hop_field_names), table, referring_model, path))
                added_paths.add(path)
                try:
                    query.query = query.query.select(
                        *[
                            table[model._meta.fields_db_projection[hop_field_name]].as_(
                                LookupPaths.safe_select_label(table.get_table_name(), hop_field_name)
                            )
                            for hop_field_name in hop_field_names
                        ]
                    )
                except KeyError as error:
                    raise FieldError(f"Unknown field {error.args[0]!r} for model {model.__name__!r}") from error

    @staticmethod
    def prefetch_map_required_local_fields(query: ModelRowsQuery[Any]) -> set[str]:
        """The fields ``.prefetch_related()`` needs on each instance - the key column of a forward
        relation, the primary key for a reverse or many-to-many one - selected whatever
        ``.only()``/``.defer()`` say. Covers relation names and ``Prefetch(...)`` objects, and a nested
        prefetch through a relation ``.only()`` loads (``only("author__name")`` beside
        ``prefetch_related("author__publisher")``) - the fields it needs on that relation's instances.

        Args:
            query: The model rows query.
        """
        relation_names = query._prefetch_map.keys() | query._prefetch_queries.keys()
        required = OnlyDeferFields.get_prefetch_key_field_names(query.model, relation_names)
        loaded_paths = set()
        for expression in query._fields_for_select:
            relation_path = expression.rpartition("__")[0]
            while relation_path:
                loaded_paths.add(relation_path)
                relation_path = relation_path.rpartition("__")[0]
        for relation_name, nested_lookups in query._prefetch_map.items():
            required.update(
                OnlyDeferFields.get_nested_prefetch_field_paths(
                    query.model, relation_name, nested_lookups, loaded_paths
                )
            )
        return required

    @staticmethod
    def get_prefetch_key_field_names(model: type[Model], relation_names: Iterable[str]) -> set[str]:
        """A model's fields its relations' prefetches match rows by.

        Args:
            model: The model.
            relation_names: Its prefetched relations.

        Returns:
            The field names.
        """
        required: set[str] = set()
        for relation_name in relation_names:
            field = model._meta.fields_map.get(relation_name)
            if isinstance(field, (ForeignKeyFieldInstance, OneToOneFieldInstance)):
                required.update(field.source_fields)
            elif isinstance(field, (BackwardForeignKeyRelation, BackwardOneToOneRelation)):
                required.update(to_field.model_field_name for to_field in field.to_field_instances)
            elif isinstance(field, ManyToManyFieldInstance):
                required.update(model._meta.primary_key_attribute_names)
        return required

    @staticmethod
    def get_nested_prefetch_field_paths(
        model: type[Model], relation_path: str, nested_lookups: Iterable[Any], loaded_paths: set[str]
    ) -> set[str]:
        """The fields a nested prefetch needs on the instances of a relation ``.only()`` loads, by path from
        the queried model (``author__publisher_id``) - nothing for a relation the prefetch loads itself.

        Args:
            model: The model the relation path starts at.
            relation_path: The relation the nested lookups go on from.
            nested_lookups: The lookups after it - names, or ``Prefetch`` objects.
            loaded_paths: The relation paths ``.only()`` loads fields of.

        Returns:
            The field paths.
        """
        if relation_path not in loaded_paths:
            return set()
        related_field = model._meta.fields_map.get(relation_path.rpartition("__")[2])
        related_model = getattr(related_field, "related_model", None)
        if related_model is None:
            return set()
        lookups_by_relation: dict[str, list[str]] = {}
        for lookup in nested_lookups:
            name = lookup if isinstance(lookup, str) else getattr(lookup, "relation", None)
            if isinstance(name, str):
                relation_name, __, rest = name.partition("__")
                lookups_by_relation.setdefault(relation_name, []).extend([rest] if rest else [])
        paths = {
            f"{relation_path}__{field_name}"
            for field_name in OnlyDeferFields.get_prefetch_key_field_names(related_model, lookups_by_relation)
        }
        for relation_name, rest_lookups in lookups_by_relation.items():
            paths.update(
                OnlyDeferFields.get_nested_prefetch_field_paths(
                    related_model, f"{relation_path}__{relation_name}", rest_lookups, loaded_paths
                )
            )
        return paths

    @staticmethod
    def select_related_required_local_fields(query: ModelRowsQuery[Any]) -> set[str]:
        """The base model's key columns an explicit ``.select_related()`` relation needs on each
        instance, selected whatever ``.only()`` says. A relation joined only by its
        ``lazy="joined"`` default isn't covered - ``.only()`` not naming it opts out. Only the first
        hop of a path matters.

        Args:
            query: The model rows query.
        """
        required: set[str] = set()
        for relation_path in query._explicitly_select_related:
            field = query.model._meta.fields_map.get(relation_path.partition("__")[0])
            if isinstance(field, (ForeignKeyFieldInstance, OneToOneFieldInstance)):
                required.update(field.source_fields)
        return required

    @staticmethod
    def get_pk_select_field_names(model: type[Model]) -> tuple[str, ...]:
        """The selectable field name(s) backing ``model``'s primary key, in pk order.

        Args:
            model: The model whose primary key is selected.

        Returns:
            Keys of ``model._meta.fields_db_projection``.
        """
        meta = model._meta
        return tuple(
            name if name in meta.fields_db_projection else cast("str", meta.fields_map[name].source_field)
            for name in meta.primary_key_attribute_names
        )
