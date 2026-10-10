from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hare.fields.constants import GENERIC_FOREIGN_KEY_TYPE_SUFFIX
from hare.fields.relations.fields.generic_foreign_key_field_instance import GenericForeignKeyFieldInstance
from hare.query.expressions.case.case import Case
from hare.query.expressions.case.when import When
from hare.query.expressions.conditions.q import Q
from hare.query.generic_foreign_keys.constants import GENERIC_FOREIGN_KEY_TYPE_KEY
from hare.query.generic_foreign_keys.generic_foreign_key_paths import GenericForeignKeyPaths

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.models import Model
    from hare.query.queryset.queryset import QuerySet


class GenericForeignKeyTypes:
    """The type of a ``GenericForeignKeyField`` - the name of the branch set, NULL for none - read
    by ``values("target__type")`` and ordered by ``order_by("target__type")`` as a ``CASE``."""

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
    def get_pending_type_aliases(queryset: QuerySet[Any, Any], names: tuple[Any, ...]) -> dict[str, Any]:
        """The ``CASE`` of each name reading a generic foreign key's type (``target__type``) the
        queryset doesn't hold yet - added as an alias under that name before the name is read.

        Args:
            queryset: The queryset.
            names: Field names or orderings.

        Returns:
            Name to its expression.
        """
        for name in names:
            # A type is read as <field>__type - checked here first, most names read none.
            if isinstance(name, str) and name.endswith(GENERIC_FOREIGN_KEY_TYPE_SUFFIX):
                break
        else:
            return {}
        return {
            name: expression
            for name, expression in GenericForeignKeyTypes.get_type_aliases(queryset.model, names).items()
            if name not in queryset._annotations
        }

    @staticmethod
    def get_type_aliases(model: type[Model], names: tuple[Any, ...]) -> dict[str, Case]:
        """The ``CASE`` of each name reading the type of a generic foreign key, under that name - an
        ordering name's leading ``-`` left out.

        Args:
            model: The queryset's model.
            names: Field names or orderings - anything but a string is skipped.

        Returns:
            Name to its expression; empty when no name reads a type.
        """
        if not GenericForeignKeyFieldInstance.declared_names:
            return {}
        aliases: dict[str, Case] = {}
        for name in names:
            if not isinstance(name, str):
                continue
            path = name.removeprefix("-")
            if "__" not in path:
                # A type is read as <field>__type - a name without "__" reads none.
                continue
            generic_path = GenericForeignKeyPaths.get_generic_path(model, path)
            if generic_path is None:
                continue
            relation_path, generic_field, rest = generic_path
            if rest == [GENERIC_FOREIGN_KEY_TYPE_KEY]:
                prefix = f"{relation_path}__" if relation_path else ""
                aliases[path] = GenericForeignKeyTypes.get_type_expression(prefix, generic_field)
        return aliases
