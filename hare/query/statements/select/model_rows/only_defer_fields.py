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
from hare.query.lookup_info.lookup_path import LookupPath
from hare.query.lookup_info.lookup_paths import LookupPaths
from hare.query.statements.building.query_joins import QueryJoins

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.fields.relations.fields.relational_field import RelationalField
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

            own_field_count = sum(1 for field in data_fields if is_real_field(field))
            query._select_related_positions.append(
                (
                    query.model,
                    own_field_count,
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
                raise FieldError(f'Unknown field "{error.args[0]}" for model "{query.model.__name__}"') from error

        else:
            # even though no data fields are selected, we need to let the executor know
            # that an empty instance of the model has to be created
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
            # The relation's extra_condition goes into this JOIN too - it is built before
            # select_related's, and the first JOIN is the one kept.
            extra_condition = query._select_related_extra_conditions.get(fetch_fields_lookup)
            model: type[Model] = query.model
            referring_model = model
            table = query.model._meta.basetable
            path: tuple[str | None, ...] = (None,)
            for position, fetch_field in enumerate(fetch_fields):
                field = cast("RelationalField[Model]", fetch_field)
                path = path + (field.model_field_name,)
                is_last_field = position == len(fetch_fields) - 1
                table = QueryJoins.join_table_by_field(
                    query, table, field.model_field_name, field, extra_condition if is_last_field else None
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
                    raise FieldError(f'Unknown field "{error.args[0]}" for model "{model.__name__}"') from error

    @staticmethod
    def prefetch_map_required_local_fields(query: ModelRowsQuery[Any]) -> set[str]:
        """The base model's fields ``.prefetch_related()`` needs on each instance - the key column of a
        forward relation, the primary key for a reverse or many-to-many one - selected whatever
        ``.only()``/``.defer()`` say. Covers relation names and ``Prefetch(...)`` objects.

        Args:
            query: The model rows query.
        """
        required: set[str] = set()
        for field_name in query._prefetch_map.keys() | query._prefetch_queries.keys():
            field = query.model._meta.fields_map[field_name]
            if isinstance(field, (ForeignKeyFieldInstance, OneToOneFieldInstance)):
                required.update(field.source_fields)
            elif isinstance(field, (BackwardForeignKeyRelation, BackwardOneToOneRelation)):
                required.update(to_field.model_field_name for to_field in field.to_field_instances)
            elif isinstance(field, ManyToManyFieldInstance):
                required.add(cast("str", query.model._meta.primary_key_attribute))
        return required

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
