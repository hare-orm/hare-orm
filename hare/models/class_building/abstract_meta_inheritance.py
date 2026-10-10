from __future__ import annotations

from copy import deepcopy
from typing import TYPE_CHECKING, Any

from hare.models.constants import ADDITIVE_ABSTRACT_META_KEYS
from hare.models.enums import ModelOption

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.models.model import Model


class AbstractMetaInheritance:
    """The ``Meta`` attributes a model class inherits from its abstract ancestors. A concrete
    ancestor's ``Meta`` isn't inherited - its table would map two models onto one table."""

    @staticmethod
    def get_merged_meta_class(
        meta_class: type[Model.Meta], bases: tuple[type, ...], canonical_mro: tuple[type, ...]
    ) -> type[Model.Meta]:
        """The ``Meta`` of a class with the attributes of its abstract ancestors' merged in.

        Args:
            meta_class: The class's own ``Meta``.
            bases: The base classes, in the order given to the ``class`` statement.
            canonical_mro: The C3 order of the ancestors, nearest first.

        Returns:
            The merged ``Meta``, the class's own when it inherits nothing.
        """
        inherited_meta_attributes = {
            **AbstractMetaInheritance.get_overridden_attributes(canonical_mro),
            **AbstractMetaInheritance.get_additive_attributes(bases),
        }
        if not inherited_meta_attributes:
            return meta_class
        own_meta_attributes = {
            option_name: option_value
            for option_name, option_value in vars(meta_class).items()
            if not option_name.startswith("__")
        }
        return type("Meta", (), {**inherited_meta_attributes, **own_meta_attributes})

    @staticmethod
    def is_abstract_meta(ancestor_meta: Any) -> bool:
        """Whether an ancestor's ``Meta`` is inherited.

        Args:
            ancestor_meta: The ancestor's own ``Meta``, None when it declares none.

        Returns:
            True for the ``Meta`` of an abstract model.
        """
        return ancestor_meta is not None and bool(getattr(ancestor_meta, ModelOption.ABSTRACT, False))

    @staticmethod
    def get_overridden_attributes(canonical_mro: tuple[type, ...]) -> dict[str, Any]:
        """The attributes the nearest ancestor declaring them wins - all except ``abstract``, which
        would make every subclass abstract, and the additive ones.

        In a diamond, a base overriding a value wins over a sibling that only passes the common
        ancestor's value through, whatever their order in the bases.

        Args:
            canonical_mro: The C3 order of the ancestors, nearest first.

        Returns:
            The attributes by name.
        """
        inherited_meta_attributes: dict[str, Any] = {}
        for ancestor in canonical_mro:
            ancestor_meta = ancestor.__dict__.get("Meta")
            if not AbstractMetaInheritance.is_abstract_meta(ancestor_meta):
                continue
            for key, value in vars(ancestor_meta).items():
                if key.startswith("__") or key in ("abstract", *ADDITIVE_ABSTRACT_META_KEYS):
                    continue
                # The nearest ancestor comes first: a name already there is a nearer ancestor's.
                if key not in inherited_meta_attributes:
                    inherited_meta_attributes[key] = value
        return inherited_meta_attributes

    @staticmethod
    def get_additive_attributes(bases: tuple[type, ...]) -> dict[str, Any]:
        """The attributes collected per base and concatenated across the bases. Within one base's
        own ancestor chain a redeclared attribute still replaces the ancestor's value.

        Args:
            bases: The base classes, in the order given to the ``class`` statement.

        Returns:
            The attributes by name.
        """
        additive_values_by_key: dict[str, list[Any]] = {}
        for base in bases:
            base_meta_attributes: dict[str, Any] = {}
            for ancestor in reversed(base.__mro__):
                ancestor_meta = ancestor.__dict__.get("Meta")
                if not AbstractMetaInheritance.is_abstract_meta(ancestor_meta):
                    continue
                base_meta_attributes.update(
                    (key, value) for key, value in vars(ancestor_meta).items() if key in ADDITIVE_ABSTRACT_META_KEYS
                )
            for key, value in base_meta_attributes.items():
                if not value:
                    continue
                entries = additive_values_by_key.setdefault(key, [])
                entries.extend(item for item in value if item not in entries)
        additive_attributes: dict[str, Any] = {}
        for key, entries in additive_values_by_key.items():
            # Deep-copied per concrete class - an Index resolves its expressions against the first
            # model asking. Copied after the merge, so an entry reached through two bases of a
            # diamond is kept once.
            copied_entries = [deepcopy(entry) for entry in entries]
            additive_attributes[key] = copied_entries if key == ModelOption.INDEXES else tuple(copied_entries)
        return additive_attributes
