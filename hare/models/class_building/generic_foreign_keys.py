from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from hare.ddl.constraints.unique_constraint import UniqueConstraint
from hare.ddl.indexes.index import Index
from hare.exceptions import ConfigurationError, QueryError
from hare.fields.relations.fields.generic_foreign_key_field_instance import GenericForeignKeyFieldInstance
from hare.fields.relations.swappable_model_reference import SwappableModelReference

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.models import Model
    from hare.models.meta_info import MetaInfo


class GenericForeignKeys:
    """Builds the generic foreign keys of a model class: each one's branches become fields of the
    model, its ``CHECK`` a constraint, and a unique constraint or index naming it one per branch."""

    @staticmethod
    def expand_declarations(
        attributes: dict[str, Any], model_name: str, is_abstract: bool
    ) -> tuple[dict[str, Any], dict[str, GenericForeignKeyFieldInstance[Any]]]:
        """Puts the branches of every generic foreign key declared in ``attrs`` right before it.

        Args:
            attributes: The class attributes - own and inherited.
            model_name: The model's name, for an error.
            is_abstract: Whether the model is abstract - it keeps the declarations alone, every
                concrete subclass gets the branches.

        Returns:
            The attributes with the branches, and the generic foreign keys by name.

        Raises:
            ConfigurationError: A branch name is taken by another attribute.
        """
        generic_fields = {
            name: value for name, value in attributes.items() if isinstance(value, GenericForeignKeyFieldInstance)
        }
        if not generic_fields:
            return attributes, generic_fields
        expanded_attributes: dict[str, Any] = {}
        for name, value in attributes.items():
            if name in generic_fields and not is_abstract and isinstance(value.to, dict):
                value.model_field_name = name
                for branch_name, branch in value.get_branch_fields(value.to).items():
                    if branch_name in attributes or branch_name in expanded_attributes:
                        raise ConfigurationError(
                            f"{model_name}.{name}: the branch {branch_name!r} is the name of another attribute of "
                            f"{model_name} - name the branch otherwise"
                        )
                    expanded_attributes[branch_name] = branch
            if name in generic_fields:
                value.model_field_name = name
            expanded_attributes[name] = value
        return expanded_attributes, generic_fields

    @staticmethod
    def copy_inherited(base: type, attributes: dict[str, Any]) -> None:
        """Copies the generic foreign keys of a base - an abstract model or a mixin - into the
        attributes of a class being built; a name already there is kept.

        Args:
            base: The base.
            attributes: The attributes collected.
        """
        meta = getattr(base, "_meta", None)
        declarations = (
            meta.generic_foreign_key_fields
            if meta is not None
            else {
                name: value
                for name, value in base.__dict__.items()
                if isinstance(value, GenericForeignKeyFieldInstance)
            }
        )
        for name, value in declarations.items():
            if name not in attributes:
                attributes[name] = deepcopy(value)

    @staticmethod
    def bind(
        model: type[Model], generic_fields: dict[str, GenericForeignKeyFieldInstance[Any]], is_abstract: bool
    ) -> None:
        """Binds the generic foreign keys to the model built: each one's ``CHECK`` joins the
        model's constraints, and a unique constraint or index naming it becomes one per branch.

        Args:
            model: The model.
            generic_fields: Its generic foreign keys by name.
            is_abstract: Whether the model is abstract.
        """
        meta = model._meta
        meta.generic_foreign_key_fields = generic_fields
        if not generic_fields:
            return
        for generic_field in generic_fields.values():
            generic_field.model = model
        if is_abstract:
            return
        GenericForeignKeyFieldInstance.declared_names.update(generic_fields)
        branched_fields = {name: field for name, field in generic_fields.items() if field.branch_names}
        GenericForeignKeys.expand_schema_entries(meta, branched_fields)

    @staticmethod
    def expand_schema_entries(meta: MetaInfo, generic_fields: dict[str, GenericForeignKeyFieldInstance[Any]]) -> None:
        """Adds the ``CHECK`` of each generic foreign key with its branches known, and turns every
        unique constraint and index naming one into one per branch.

        Args:
            meta: The model's meta.
            generic_fields: The generic foreign keys whose branches exist.
        """
        if not generic_fields:
            return
        constraints: list[Any] = []
        for constraint in meta.constraints:
            if isinstance(constraint, UniqueConstraint):
                constraints.extend(GenericForeignKeys.get_unique_constraint_per_branch(constraint, generic_fields))
            else:
                constraints.append(constraint)
        constraints.extend(generic_field.get_check_constraint() for generic_field in generic_fields.values())
        meta.constraints = tuple(constraints)
        indexes: list[tuple[str, ...] | Index] = []
        for index in meta.indexes:
            indexes.extend(GenericForeignKeys.get_index_per_branch(index, generic_fields))
        meta.indexes = tuple(indexes)

    @staticmethod
    def get_branch_combinations(
        field_names: tuple[str, ...] | list[str], generic_fields: dict[str, GenericForeignKeyFieldInstance[Any]]
    ) -> list[tuple[str, list[str]]]:
        """Every field list a list naming generic foreign keys stands for - one per combination of
        their branches.

        Args:
            field_names: The field names.
            generic_fields: The generic foreign keys.

        Returns:
            ``(name suffix, field names)`` per combination - a single ``("", field_names)`` when none
            is named.
        """
        combinations: list[tuple[str, list[str]]] = [("", [])]
        for field_name in field_names:
            generic_field = generic_fields.get(field_name)
            if generic_field is None:
                combinations = [(suffix, [*names, field_name]) for suffix, names in combinations]
            else:
                combinations = [
                    (f"{suffix}_{branch_name}", [*names, branch_name])
                    for suffix, names in combinations
                    for branch_name in generic_field.branch_names
                ]
        return combinations

    @staticmethod
    def get_unique_constraint_per_branch(
        constraint: UniqueConstraint, generic_fields: dict[str, GenericForeignKeyFieldInstance[Any]]
    ) -> list[UniqueConstraint]:
        """A unique constraint naming generic foreign keys as one per branch - named with the
        branch appended.

        Args:
            constraint: The constraint.
            generic_fields: The generic foreign keys.

        Returns:
            The constraints.
        """
        if not any(field_name in generic_fields for field_name in constraint.fields):
            return [constraint]
        return [
            replace(constraint, fields=tuple(names), name=f"{constraint.name}{suffix}" if constraint.name else None)
            for suffix, names in GenericForeignKeys.get_branch_combinations(constraint.fields, generic_fields)
        ]

    @staticmethod
    def get_index_per_branch(
        index: tuple[str, ...] | Index, generic_fields: dict[str, GenericForeignKeyFieldInstance[Any]]
    ) -> list[tuple[str, ...] | Index]:
        """An index naming generic foreign keys as one per branch - named with the branch appended.

        Args:
            index: The index - a field tuple or an ``Index``.
            generic_fields: The generic foreign keys.

        Returns:
            The indexes.
        """
        field_names = index.fields if isinstance(index, Index) else index
        if not any(field_name in generic_fields for field_name in field_names):
            return [index]
        if not isinstance(index, Index):
            return [
                tuple(names) for _suffix, names in GenericForeignKeys.get_branch_combinations(index, generic_fields)
            ]
        branch_indexes: list[tuple[str, ...] | Index] = []
        for suffix, names in GenericForeignKeys.get_branch_combinations(index.fields, generic_fields):
            branch_index = deepcopy(index)
            branch_index.fields = names
            if index.name:
                branch_index.name = f"{index.name}{suffix}"
            branch_indexes.append(branch_index)
        return branch_indexes

    @staticmethod
    def expand_field_names(meta: MetaInfo, field_names: Any) -> Any:
        """Field names with each generic foreign key given as its branches.

        Args:
            meta: The model's meta.
            field_names: The names - anything but a collection of them is returned as it is.

        Returns:
            The names.
        """
        generic_fields = meta.generic_foreign_key_fields
        if not generic_fields or isinstance(field_names, (str, bytes)):
            return field_names
        expanded_names: list[str] = []
        for field_name in field_names:
            generic_field = generic_fields.get(field_name)
            if generic_field is None:
                expanded_names.append(field_name)
            else:
                expanded_names.extend(generic_field.branch_names)
        return expanded_names

    @staticmethod
    def expand_kwargs(meta: MetaInfo, kwargs: dict[str, Any], *, apply_defaults: bool) -> dict[str, Any]:
        """Field values given by name with each generic foreign key's value given as the values of
        its branches.

        Args:
            meta: The model's meta.
            kwargs: The values by name.
            apply_defaults: Whether a generic foreign key given no value - nor any branch - takes
                its ``default``: a new instance is being built.

        Returns:
            The values - ``kwargs`` itself when no generic foreign key takes part.

        Raises:
            QueryError: A generic foreign key and one of its branches are both given.
        """
        expanded_kwargs: dict[str, Any] | None = None
        for name, generic_field in meta.generic_foreign_key_fields.items():
            if not generic_field.branch_names:
                continue
            fields_map = meta.fields_map
            given_branch_keys = [
                key
                for branch_name in generic_field.branch_names
                for key in (branch_name, *fields_map[branch_name].source_fields)  # type: ignore[attr-defined]
                if key in kwargs
            ]
            if name in kwargs:
                if given_branch_keys:
                    raise QueryError(
                        f"{generic_field.get_label()} and its branch {given_branch_keys[0]!r} were both given - "
                        "give one"
                    )
                if expanded_kwargs is None:
                    expanded_kwargs = dict(kwargs)
                expanded_kwargs.update(generic_field.get_branch_values(expanded_kwargs.pop(name)))
            elif apply_defaults and not given_branch_keys and generic_field.default is not None:
                if expanded_kwargs is None:
                    expanded_kwargs = dict(kwargs)
                expanded_kwargs.update(generic_field.get_default_branch_values() or {})
        return kwargs if expanded_kwargs is None else expanded_kwargs

    @staticmethod
    def expand_swappable_targets(model: type[Model], targets_by_setting: dict[str, dict[str, str]]) -> None:
        """Builds the branches of the model's generic foreign keys whose targets are a ``swappable``
        setting - known only once the configuration is.

        Args:
            model: The model.
            targets_by_setting: Setting name to its targets - branch name to ``"app.Model"``.

        Raises:
            ConfigurationError: A setting isn't configured, or a branch name is taken.
        """
        meta = model._meta
        expanded: dict[str, GenericForeignKeyFieldInstance[Any]] = {}
        for name, generic_field in meta.generic_foreign_key_fields.items():
            if not isinstance(generic_field.to, SwappableModelReference) or generic_field.branch_names:
                continue
            setting = generic_field.to.setting
            targets = targets_by_setting.get(setting)
            if targets is None:
                raise ConfigurationError(
                    f'{generic_field.get_label()}: the swappable setting "{setting}" isn\'t configured - set it to a '
                    'dict of branch name to "app.Model" in the "swappable" config section'
                )
            GenericForeignKeyFieldInstance.check_targets(targets)
            for branch_name, branch in generic_field.get_branch_fields(dict(targets)).items():
                if branch_name in meta.fields_map or hasattr(model, branch_name):
                    raise ConfigurationError(
                        f"{generic_field.get_label()}: the branch {branch_name!r} is the name of another attribute of "
                        f"{model.__name__} - name the branch otherwise"
                    )
                meta.add_field(branch_name, branch)
            generic_field.branch_names = tuple(targets)
            expanded[name] = generic_field
        GenericForeignKeys.expand_schema_entries(meta, expanded)
