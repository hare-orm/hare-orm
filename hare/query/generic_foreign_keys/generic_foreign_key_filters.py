from __future__ import annotations

from copy import copy
from typing import TYPE_CHECKING, Any

from hare.fields.relations.fields.generic_foreign_key_field_instance import GenericForeignKeyFieldInstance
from hare.query.enums import Connector, Lookup
from hare.query.expressions.conditions.q import Q
from hare.query.generic_foreign_keys.constants import (
    GENERIC_FOREIGN_KEY_LIST_LOOKUPS,
    GENERIC_FOREIGN_KEY_NEGATED_LOOKUPS,
    GENERIC_FOREIGN_KEY_NULL_LOOKUPS,
)
from hare.query.generic_foreign_keys.generic_foreign_key_paths import GenericForeignKeyPaths

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.models import Model


class GenericForeignKeyFilters:
    """Turns the filters naming a ``GenericForeignKeyField`` into filters over its branches, before
    a query is planned or built - ``target=post`` is ``post=post``, ``target__type="photo"`` is
    ``photo_id__isnull=False``. A path may reach the field through relations
    (``comment__target__type``)."""

    @staticmethod
    def get_branch_q(model: type[Model], key: str, value: Any) -> Q | None:
        """The condition over the branches a filter on a generic foreign key stands for.

        Args:
            model: The model the filter is resolved on.
            key: The filter key.
            value: Its value.

        Returns:
            The condition; None when the key names no generic foreign key.

        Raises:
            FieldError: A lookup the field doesn't have.
        """
        filter_parts = GenericForeignKeyPaths.get_filter_parts(model, key)
        if filter_parts is None:
            return None
        relation_path, generic_field, compares_type, lookup = filter_parts
        prefix = f"{relation_path}__" if relation_path else ""
        if lookup in GENERIC_FOREIGN_KEY_NULL_LOOKUPS:
            is_null = bool(value) != (lookup == Lookup.NOT_ISNULL)
            return GenericForeignKeyFilters.get_isnull_q(prefix, generic_field, is_null)
        values = list(value) if lookup in GENERIC_FOREIGN_KEY_LIST_LOOKUPS else [value]
        if compares_type:
            condition = GenericForeignKeyFilters.get_type_q(prefix, generic_field, values)
        else:
            condition = GenericForeignKeyFilters.get_objects_q(prefix, generic_field, values)
        return ~condition if lookup in GENERIC_FOREIGN_KEY_NEGATED_LOOKUPS else condition

    @staticmethod
    def get_isnull_q(prefix: str, generic_field: GenericForeignKeyFieldInstance[Any], is_null: bool) -> Q:
        """No branch set (``is_null``) or one set.

        Args:
            prefix: The relation path before the field, ending in ``__``.
            generic_field: The field.
            is_null: Whether no branch is set.

        Returns:
            The condition.
        """
        if is_null:
            return Q(
                **{
                    f"{prefix}{generic_field.get_branch_column(name)}__isnull": True
                    for name in generic_field.branch_names
                }
            )
        return Q.with_connector(
            Connector.OR,
            *(
                Q(**{f"{prefix}{generic_field.get_branch_column(name)}__isnull": False})
                for name in generic_field.branch_names
            ),
        )

    @staticmethod
    def get_never_q(prefix: str, generic_field: GenericForeignKeyFieldInstance[Any]) -> Q:
        """A condition no row meets."""
        return Q(**{f"{prefix}{generic_field.branch_names[0]}__in": []})

    @staticmethod
    def get_type_q(prefix: str, generic_field: GenericForeignKeyFieldInstance[Any], types: list[Any]) -> Q:
        """The branch set is one of ``types`` - a type naming no branch matches nothing.

        Args:
            prefix: The relation path before the field, ending in ``__``.
            generic_field: The field.
            types: Branch names - None for no branch set.

        Returns:
            The condition.
        """
        conditions = [
            Q(**{f"{prefix}{generic_field.get_branch_column(branch_type)}__isnull": False})
            for branch_type in dict.fromkeys(types)
            if branch_type in generic_field.branch_names
        ]
        if None in types:
            conditions.append(GenericForeignKeyFilters.get_isnull_q(prefix, generic_field, True))
        if not conditions:
            return GenericForeignKeyFilters.get_never_q(prefix, generic_field)
        return conditions[0] if len(conditions) == 1 else Q.with_connector(Connector.OR, *conditions)

    @staticmethod
    def get_objects_q(prefix: str, generic_field: GenericForeignKeyFieldInstance[Any], values: list[Any]) -> Q:
        """The field is one of ``values`` - each object compared on its own branch.

        Args:
            prefix: The relation path before the field, ending in ``__``.
            generic_field: The field.
            values: Objects of the targets or ``{"type": ..., <key>}`` - None for no branch set.

        Returns:
            The condition.

        Raises:
            ValidationError: An object isn't of a target model.
        """
        values_by_branch: dict[str, list[Any]] = {}
        conditions: list[Q] = []
        for value in values:
            if value is None:
                continue
            if isinstance(value, dict):
                # {"type": "post", "id": 1} - the key columns of its branch.
                column_values = generic_field.get_column_values(value)
                conditions.append(
                    Q(
                        **{
                            f"{prefix}{column}": column_value
                            for column, column_value in column_values.items()
                            if column_value is not None
                        }
                    )
                )
                continue
            values_by_branch.setdefault(generic_field.get_branch_of(value), []).append(value)
        conditions.extend(
            Q(**{f"{prefix}{branch_name}": branch_values[0]})
            if len(branch_values) == 1
            else Q(**{f"{prefix}{branch_name}__in": branch_values})
            for branch_name, branch_values in values_by_branch.items()
        )
        if None in values:
            conditions.append(GenericForeignKeyFilters.get_isnull_q(prefix, generic_field, True))
        if not conditions:
            return GenericForeignKeyFilters.get_never_q(prefix, generic_field)
        return conditions[0] if len(conditions) == 1 else Q.with_connector(Connector.OR, *conditions)

    @staticmethod
    def rewrite_q(model: type[Model], condition: Q) -> Q:
        """A ``Q`` tree with every filter naming a generic foreign key turned into its condition over
        the branches.

        Args:
            model: The model the condition is resolved on.
            condition: The condition.

        Returns:
            The rewritten copy - ``condition`` itself when nothing names a generic foreign key.
        """
        # Loops and list comprehensions, not generators: this runs for every condition of a filter
        # once any model declares a generic foreign key.
        if condition.children:
            children = [GenericForeignKeyFilters.rewrite_q(model, child) for child in condition.children]
            changed = False
            for child, original in zip(children, condition.children, strict=True):
                if child is not original:
                    changed = True
                    break
            if not changed:
                return condition
            rewritten = copy(condition)
            rewritten.children = tuple(children)
            return rewritten
        if not condition.filters:
            return condition
        declared_names = GenericForeignKeyFieldInstance.declared_names
        branch_conditions = {
            key: branch_q
            for key, value in condition.filters.items()
            # Checked here first: most keys name no generic foreign key.
            if (key in declared_names or ("__" in key and not declared_names.isdisjoint(key.split("__"))))
            and (branch_q := GenericForeignKeyFilters.get_branch_q(model, key, value)) is not None
        }
        if not branch_conditions:
            return condition
        generation = condition._filter_call_generation
        child_conditions = [
            branch_conditions[key] if key in branch_conditions else Q(**{key: value})
            for key, value in condition.filters.items()
        ]
        rewritten = copy(condition)
        rewritten.filters = {}
        rewritten.children = tuple([child._stamp_filter_call_generation(generation) for child in child_conditions])
        return rewritten

    @staticmethod
    def rewrite_arguments(
        model: type[Model], conditions: tuple[Any, ...], kwargs: dict[str, Any]
    ) -> tuple[tuple[Any, ...], dict[str, Any]]:
        """The arguments of ``filter()``/``exclude()``/``get()`` with every filter naming a generic
        foreign key turned into its condition over the branches - such a keyword argument becomes
        a ``Q`` argument.

        Args:
            model: The queryset's model.
            conditions: The ``Q``/``Exists`` arguments.
            kwargs: The keyword filters.

        Returns:
            The conditions and the keyword filters - the ones given when nothing names a generic
            foreign key.
        """
        if not GenericForeignKeyFieldInstance.declared_names:
            return conditions, kwargs
        changed = False
        rewritten_conditions = conditions
        if conditions:
            rewritten_list = [
                GenericForeignKeyFilters.rewrite_q(model, condition) if isinstance(condition, Q) else condition
                for condition in conditions
            ]
            for rewritten, original in zip(rewritten_list, conditions, strict=True):
                if rewritten is not original:
                    changed = True
                    rewritten_conditions = tuple(rewritten_list)
                    break
        declared_names = GenericForeignKeyFieldInstance.declared_names
        branch_conditions: list[Q] = []
        plain_kwargs: dict[str, Any] = {}
        for key, value in kwargs.items():
            branch_q = (
                GenericForeignKeyFilters.get_branch_q(model, key, value)
                if key in declared_names or ("__" in key and not declared_names.isdisjoint(key.split("__")))
                else None
            )
            if branch_q is None:
                plain_kwargs[key] = value
            else:
                branch_conditions.append(branch_q)
        if not branch_conditions and not changed:
            return conditions, kwargs
        return (*rewritten_conditions, *branch_conditions), plain_kwargs
