from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hare.exceptions import FieldError
from hare.fields.relations.fields.generic_foreign_key_field_instance import GenericForeignKeyFieldInstance
from hare.fields.relations.fields.relational_field import RelationalField
from hare.query.enums import Lookup, LookupTarget, LookupValueShape
from hare.query.generic_foreign_keys.constants import (
    GENERIC_FOREIGN_KEY_LIST_LOOKUPS,
    GENERIC_FOREIGN_KEY_LOOKUPS,
    GENERIC_FOREIGN_KEY_NULL_LOOKUPS,
    GENERIC_FOREIGN_KEY_TYPE_KEY,
)
from hare.query.lookup_info.lookup_info import LookupInfo

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.models import Model


class GenericForeignKeyPaths:
    """Finds the ``GenericForeignKeyField`` a path of a query names - itself or through relations
    (``comment__target``) - and turns a relation path ending at one into the paths of its
    branches."""

    @staticmethod
    def get_generic_path(
        model: type[Model], path: str
    ) -> tuple[str, GenericForeignKeyFieldInstance[Any], list[str]] | None:
        """Finds the generic foreign key a path names.

        Args:
            model: The model the path is resolved on.
            path: The path - a filter key, a relation or field name.

        Returns:
            The relations before the field (``""`` for the model's own), the field and the parts
            after it; None when the path names none.
        """
        declared_names = GenericForeignKeyFieldInstance.declared_names
        if not declared_names:
            return None
        segments = path.split("__")
        if not declared_names.intersection(segments):
            return None
        current_model = model
        for index, segment in enumerate(segments):
            meta = current_model._meta
            generic_field = meta.generic_foreign_key_fields.get(segment)
            if generic_field is not None and generic_field.branch_names:
                return "__".join(segments[:index]), generic_field, segments[index + 1 :]
            field = meta.fields_map.get(segment)
            if not isinstance(field, RelationalField):
                return None
            current_model = field.related_model
        return None

    @staticmethod
    def get_filter_parts(
        model: type[Model], key: str
    ) -> tuple[str, GenericForeignKeyFieldInstance[Any], bool, Lookup] | None:
        """Reads a filter key naming a generic foreign key.

        Args:
            model: The model the key is resolved on.
            key: The filter key.

        Returns:
            The relations before the field, the field, whether the key compares its type, and the
            lookup; None when the key names no generic foreign key.

        Raises:
            FieldError: A lookup the field doesn't take.
        """
        generic_path = GenericForeignKeyPaths.get_generic_path(model, key)
        if generic_path is None:
            return None
        relation_path, generic_field, rest = generic_path
        compares_type = rest[:1] == [GENERIC_FOREIGN_KEY_TYPE_KEY]
        lookup_parts = rest[1:] if compares_type else rest
        lookup_name = lookup_parts[0] if len(lookup_parts) == 1 else "" if not lookup_parts else None
        if lookup_name not in GENERIC_FOREIGN_KEY_LOOKUPS:
            raise FieldError(
                f"Unknown filter param '{key}' - {generic_field.get_label()} takes =, __not, __in, __not_in, "
                "__isnull and __not_isnull, on itself and on its __type"
            )
        return relation_path, generic_field, compares_type, Lookup(lookup_name)

    @staticmethod
    def get_lookup_info(model: type[Model], key: str) -> LookupInfo | None:
        """Describes a filter key naming a generic foreign key.

        Args:
            model: The model the key is resolved on.
            key: The filter key.

        Returns:
            The description - the value is an object of a target (or ``{"type": ..., <key>}``),
            the name of a branch for ``__type``, a bool for ``__isnull``; None when the key names
            no generic foreign key.

        Raises:
            FieldError: A lookup the field doesn't take.
        """
        filter_parts = GenericForeignKeyPaths.get_filter_parts(model, key)
        if filter_parts is None:
            return None
        relation_path, generic_field, compares_type, lookup = filter_parts
        relations: list[Any] = []
        current_model = model
        for segment in relation_path.split("__") if relation_path else ():
            relation = current_model._meta.fields_map[segment]
            relations.append(relation)
            current_model = relation.related_model  # type: ignore[attr-defined]
        if lookup in GENERIC_FOREIGN_KEY_NULL_LOOKUPS:
            value_type: Any = bool
        else:
            value_type = str if compares_type else dict
        return LookupInfo(
            key=key,
            model=model,
            relations=tuple(relations),
            field=None,
            transforms=(),
            lookup=lookup,
            value_shape=LookupValueShape.LIST
            if lookup in GENERIC_FOREIGN_KEY_LIST_LOOKUPS
            else LookupValueShape.VALUE,
            value_type=value_type,
            crosses_to_many=any(relation.is_multi_valued for relation in relations),
            requires_extension=None,
            dialects=None,
            target=LookupTarget.GENERIC_RELATION,
            generic_field=generic_field,
            compares_generic_type=compares_type,
        )

    @staticmethod
    def expand_relation_paths(model: type[Model], paths: tuple[Any, ...], method_name: str) -> tuple[Any, ...]:
        """Relation paths with each one ending at a generic foreign key given as the paths of its
        branches - ``select_related("target")`` is ``select_related("post", "photo")``.

        Args:
            model: The queryset's model.
            paths: The paths - anything but a string is kept as it is.
            method_name: The method given them, for an error.

        Returns:
            The paths - ``paths`` itself when none ends at a generic foreign key.

        Raises:
            FieldError: A path goes on past a generic foreign key.
        """
        if not GenericForeignKeyFieldInstance.declared_names:
            return paths
        expanded_paths: list[Any] = []
        changed = False
        for path in paths:
            generic_path = GenericForeignKeyPaths.get_generic_path(model, path) if isinstance(path, str) else None
            if generic_path is None:
                expanded_paths.append(path)
                continue
            relation_path, generic_field, rest = generic_path
            if rest:
                raise FieldError(
                    f"{method_name}() can't go on past {generic_field.get_label()} - its targets differ; name the "
                    f"branch instead ({', '.join(repr(name) for name in generic_field.branch_names)})"
                )
            prefix = f"{relation_path}__" if relation_path else ""
            expanded_paths.extend(f"{prefix}{branch_name}" for branch_name in generic_field.branch_names)
            changed = True
        return tuple(expanded_paths) if changed else paths
