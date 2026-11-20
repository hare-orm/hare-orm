from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING, Any, ClassVar, TypeVar

from hare.classes.deferred_import import DeferredImport
from hare.fields.constants import GENERIC_FOREIGN_KEY_TYPE_SUFFIX
from hare.fields.relations.fields.generic_foreign_key_field_instance import GenericForeignKeyFieldInstance
from hare.query.expressions.case.case import Case
from hare.query.expressions.case.when import When
from hare.query.expressions.conditions.q import Q
from hare.query.generic_foreign_keys.constants import GENERIC_FOREIGN_KEY_TYPE_KEY
from hare.query.generic_foreign_keys.generic_foreign_key_paths import GenericForeignKeyPaths

if TYPE_CHECKING:
    from hare.models.model_binding import ModelBinding
    from hare.query.expressions.f import F
    from hare.query.expressions.ordering import Ordering
    from hare.query.queryset.queryset import QuerySet


TQuerySet = TypeVar("TQuerySet", bound="QuerySet[Any, Any]")


class GenericForeignKeyTypes:
    """The type of a ``GenericForeignKeyField`` - the name of the branch set, NULL for none - read
    by ``values("target__type")`` and ordered by ``order_by("target__type")`` as a ``CASE``."""

    #: ``F`` - imported at its first read, as its module imports this one.
    reference_class: ClassVar[DeferredImport[type[F]]] = DeferredImport("hare.query.expressions.f", "F")
    #: ``Ordering`` - imported at its first read, as its module imports this one.
    ordering_class: ClassVar[DeferredImport[type[Ordering]]] = DeferredImport(
        "hare.query.expressions.ordering", "Ordering"
    )

    @staticmethod
    def get_type_expression(prefix: str, generic_field: GenericForeignKeyFieldInstance[Any]) -> Case:
        """The ``CASE`` giving the name of the branch set.

        Args:
            prefix: The relation path before the field, ending in ``__`` - empty for the model's own.
            generic_field: The field.

        Returns:
            The expression.
        """
        return Case(
            *(
                When(Q(**{f"{prefix}{generic_field.get_branch_column(branch_name)}__isnull": False}), then=branch_name)
                for branch_name in generic_field.branch_names
            ),
            default=None,
        )

    @staticmethod
    def get_type_aliased_queryset(queryset: TQuerySet, names: Iterable[Any]) -> TQuerySet | None:
        """Add missing aliases for names reading a generic foreign key's type (``target__type``).

        Args:
            queryset: The queryset.
            names: Field names, orderings (``"-target__type"``, ``F("target__type").asc()``) or ``F()``.

        Returns:
            The queryset with the aliases added, or None if none are needed.
        """
        field_names = GenericForeignKeyTypes.get_named_fields(names)
        for name in field_names:
            # A type is read as <field>__type - checked here first, most names read none.
            if name.endswith(GENERIC_FOREIGN_KEY_TYPE_SUFFIX):
                break
        else:
            return None
        model_binding: ModelBinding = queryset._binding  # type: ignore[assignment]
        aliases = {
            name: expression
            for name, expression in GenericForeignKeyTypes.get_type_aliases(model_binding, field_names).items()
            if name not in queryset._annotations
        }
        return queryset.alias(**aliases) if aliases else None

    @staticmethod
    def get_named_fields(names: Iterable[Any]) -> tuple[str, ...]:
        """The field names among names, orderings and references - a string, the name of an ``F()``, the
        field an ``Ordering`` orders by; anything else left out.

        Args:
            names: Field names, orderings or references.

        Returns:
            The names.
        """
        reference_class = GenericForeignKeyTypes.reference_class
        ordering_class = GenericForeignKeyTypes.ordering_class
        field_names: list[str] = []
        for name in names:
            if isinstance(name, ordering_class):
                name = name.target
            if isinstance(name, reference_class):
                name = name.name
            if isinstance(name, str):
                field_names.append(name)
        return tuple(field_names)

    @staticmethod
    def get_type_aliases(binding: ModelBinding, names: tuple[Any, ...]) -> dict[str, Case]:
        """The ``CASE`` of each name reading the type of a generic foreign key, under that name - an
        ordering name's leading ``-`` left out.

        Args:
            binding: The queryset's model.
            names: Field names or orderings - anything but a string is skipped.

        Returns:
            Name to its expression; empty when no name reads a type.
        """
        aliases: dict[str, Case] = {}
        for name in names:
            if not isinstance(name, str):
                continue
            path = name.removeprefix("-")
            if "__" not in path:
                # A type is read as <field>__type - a name without "__" reads none.
                continue
            generic_path = GenericForeignKeyPaths.get_generic_path(binding, path)
            if generic_path is None:
                continue
            relation_path, generic_field, rest = generic_path
            if rest == [GENERIC_FOREIGN_KEY_TYPE_KEY]:
                prefix = f"{relation_path}__" if relation_path else ""
                aliases[path] = GenericForeignKeyTypes.get_type_expression(prefix, generic_field)
        return aliases
