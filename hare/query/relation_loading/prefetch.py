from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

from hare.exceptions import FieldError, QueryError
from hare.query.enums import RowShape

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.models import Model
    from hare.query.queryset import QuerySet


class Prefetch:
    """A prefetch of a relation with a custom queryset. ``queryset`` runs as its own top-level query -
    an ``OuterRef`` inside it never refers to the model prefetched onto.

    Args:
        relation: The relation's name.
        queryset: The queryset to prefetch with.
        to_attr: The attribute the result is set on - not the name of a field, relation or other
            attribute of the model.

    Raises:
        QueryError: ``queryset`` isn't a queryset of model instances.
    """

    __slots__ = ("relation", "queryset", "to_attr")

    def __init__(self, relation: str, queryset: QuerySet[Any], to_attr: str | None = None) -> None:
        from hare.query.queryset import QuerySet

        if not isinstance(queryset, QuerySet):
            raise QueryError(
                f"Prefetch({relation!r}, queryset=...) needs a model QuerySet, got {type(queryset).__name__}."
            )
        if queryset._selection is not None:
            method = ".values()" if queryset._selection.shape == RowShape.DICT else ".values_list()"
            raise QueryError(
                f"Prefetch({relation!r}, queryset=...) needs a model QuerySet, got a {method} query - "
                "it yields no model instances to attach."
            )
        self.to_attr = to_attr
        self.relation = relation
        self.queryset = queryset

    @staticmethod
    def add_lookups(
        model: type[Model],
        prefetch_map: dict[str, set[str | Prefetch]],
        prefetch_queries: dict[str, list[tuple[str | None, QuerySet[Any]]]],
        lookups: Iterable[str | Prefetch],
    ) -> None:
        """Adds ``prefetch_related()`` lookups to what a query prefetches - a path through the
        ``to_attr`` of a ``Prefetch`` after that ``Prefetch``, whatever order they are given in.

        Args:
            model: The model the lookups start from.
            prefetch_map: Relation name -> the nested lookups prefetched through the plain relation.
            prefetch_queries: Relation name -> ``(to_attr, queryset)`` of each explicit prefetch.
            lookups: Relation paths and ``Prefetch`` objects.
        """
        fetch_fields = model._meta.fetch_fields
        for lookup in sorted(lookups, key=lambda lookup: Prefetch.get_first_name(lookup) not in fetch_fields):
            Prefetch.add_lookup(model, prefetch_map, prefetch_queries, lookup)

    @staticmethod
    def get_first_name(lookup: str | Prefetch) -> str:
        """The first name of a lookup's path.

        Args:
            lookup: A relation path, or a ``Prefetch``.

        Returns:
            The name.
        """
        return (lookup.relation if isinstance(lookup, Prefetch) else lookup).partition("__")[0]

    @staticmethod
    def add_lookup(
        model: type[Model],
        prefetch_map: dict[str, set[str | Prefetch]],
        prefetch_queries: dict[str, list[tuple[str | None, QuerySet[Any]]]],
        lookup: str | Prefetch,
    ) -> None:
        """Adds one ``prefetch_related()`` lookup to what a query prefetches: a path through a relation
        goes to the plain relation's prefetch, a path starting with a ``Prefetch``'s ``to_attr`` to
        that prefetch's query. A changed container is replaced, never changed in place.

        Args:
            model: The model the lookup starts from.
            prefetch_map: Relation name -> the nested lookups prefetched through the plain relation.
            prefetch_queries: Relation name -> ``(to_attr, queryset)`` of each explicit prefetch.
            lookup: A relation path, or a ``Prefetch``.

        Raises:
            FieldError: The path starts with neither a relation of the model nor a ``to_attr``.
        """
        relation_path = lookup.relation if isinstance(lookup, Prefetch) else lookup
        first_name, __, nested_path = relation_path.partition("__")
        meta = model._meta
        nested_lookup: str | Prefetch = (
            Prefetch(nested_path, lookup.queryset, to_attr=lookup.to_attr)
            if isinstance(lookup, Prefetch) and nested_path
            else nested_path
        )
        if first_name not in meta.fetch_fields:
            for field_name, entries in prefetch_queries.items():
                for index, (to_attr, queryset) in enumerate(entries):
                    if to_attr != first_name:
                        continue
                    if nested_lookup:
                        prefetch_queries[field_name] = [
                            *entries[:index],
                            (to_attr, queryset.prefetch_related(nested_lookup)),
                            *entries[index + 1 :],
                        ]
                    return
            if first_name in meta.fields_map:
                raise FieldError(f"Field {first_name} on {meta.full_name} is not a relation")
            raise FieldError(f"Relation {first_name} for {meta.full_name} not found")
        if isinstance(lookup, Prefetch):
            lookup.check_to_attr(model)
            if not nested_path:
                prefetch_queries[first_name] = [
                    *prefetch_queries.get(first_name, ()),
                    (lookup.to_attr, lookup.queryset),
                ]
                return
        nested_lookups = set(prefetch_map.get(first_name, ()))
        if nested_lookup:
            nested_lookups.add(nested_lookup)
        prefetch_map[first_name] = nested_lookups

    def check_to_attr(self, model: type[Model]) -> None:
        """Checks ``to_attr`` doesn't name a field, relation, key column or other attribute of
        the model owning the last relation of ``relation`` - the prefetch result would overwrite it
        on every instance, and ``save()`` would then write that result back.

        Args:
            model: The model ``relation`` starts from.

        Raises:
            QueryError: If ``to_attr`` collides with an attribute of that model.
        """
        to_attr = self.to_attr
        if to_attr is None:
            return
        *leading_relation_names, __ = self.relation.split("__")
        for relation_name in leading_relation_names:
            relation_field = model._meta.fields_map.get(relation_name)
            related_model = getattr(relation_field, "related_model", None)
            if related_model is None:
                return
            model = related_model
        if to_attr in model._meta.fields_map or to_attr == "pk" or hasattr(model, to_attr):
            raise QueryError(
                f"Prefetch({self.relation!r}, to_attr={to_attr!r}) conflicts with the attribute "
                f"{model.__name__}.{to_attr} - pick a to_attr that isn't a field, relation or "
                "attribute of the model."
            )
