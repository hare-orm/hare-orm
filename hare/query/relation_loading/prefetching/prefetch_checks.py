from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

from hare.exceptions import QueryError
from hare.fields.relations.fields.many_to_many_field_instance import ManyToManyFieldInstance
from hare.fields.relations.relation_values import RelationValues

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.models import Model
    from hare.query.queryset import QuerySet


class PrefetchChecks:
    """The checks made before a prefetch query runs: the instances are saved and have their relation
    keys loaded, a Prefetch() queryset is of the related model, and an .only() of it keeps the
    columns the rows are matched by."""

    @staticmethod
    def reject_many_to_many_prefetch_queryset_model_mismatch(
        related_queryset: QuerySet[Any], field: str, field_object: ManyToManyFieldInstance[Any]
    ) -> None:
        """Rejects a ``Prefetch`` queryset on a many-to-many relation that isn't of the relation's
        related model.

        Raises:
            QueryError: The queryset is of another model.
        """
        if related_queryset.model is not field_object.related_model:
            raise QueryError(
                f"Prefetch({field!r}, queryset=...) queryset must be built from "
                f"{field_object.related_model.__name__} (this relation's related model), got "
                f"{related_queryset.model.__name__} - a through-model queryset isn't supported here."
            )

    @staticmethod
    def reject_unsaved_instances(objs: Iterable[Model]) -> None:
        """Checks every instance of ``objs`` is saved - a reverse FK/O2O or M2M relation is
        keyed by the instance's own pk, which an unsaved instance doesn't have yet.

        Raises:
            QueryError: If an instance is not saved.
        """
        for instance in objs:
            if not instance._saved_in_db:
                raise QueryError(f"You should first call .save() on {instance!r}")

    @staticmethod
    def reject_unloaded_relation_keys(objs: Iterable[Model], field_names: tuple[str, ...], field: str) -> None:
        """Checks every instance loaded the fields the relation ``field`` references it by.

        Raises:
            QueryError: An instance left one of ``field_names`` unloaded (``.only()``/``.defer()``).
        """
        for instance in objs:
            # Only an instance loaded with .only()/.defer() can lack a field.
            if instance._partial:
                RelationValues.get_relation_key_values(instance, field_names, f"Fetching '{field}'")

    @staticmethod
    def ensure_only_includes_fields(related_queryset: QuerySet[Any], *required_field_names: str) -> QuerySet[Any]:
        """Adds ``required_field_names`` - the columns the prefetch matches rows by - to a ``Prefetch``
        queryset's ``.only()``/``.defer()`` restriction.

        Returns:
            ``related_queryset`` itself when it has no restriction or already selects the fields.
        """
        fields_for_select = related_queryset._fields_for_select
        if fields_for_select and not set(required_field_names) <= set(fields_for_select):
            return related_queryset.only(*fields_for_select, *required_field_names)
        # .defer() fields are expanded only when the query is built - read here directly.
        deferred_fields = related_queryset._deferred_fields
        still_deferred = set(deferred_fields) & set(required_field_names)
        if still_deferred:
            related_queryset = related_queryset._clone()
            related_queryset._deferred_fields = tuple(
                field_name for field_name in deferred_fields if field_name not in still_deferred
            )
        return related_queryset
