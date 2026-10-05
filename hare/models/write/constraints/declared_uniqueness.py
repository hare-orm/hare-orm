from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from functools import cached_property
from operator import attrgetter
from typing import TYPE_CHECKING, Any

from hare.ddl.constraints.unique_constraint import UniqueConstraint
from hare.ddl.indexes.index import Index
from hare.models.write.constraints.constants import EXACT_LOOKUP_SUFFIX, LOOKUP_SEPARATOR
from hare.query.enums import Connector
from hare.query.expressions import Q

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.models import Model


@dataclass(frozen=True)
class DeclaredUniqueness:
    """A set of fields a model declares unique - its primary key, a field of ``unique=True``, a
    ``UniqueConstraint``, an ``Index(unique=True)`` - as the attributes of an instance holding them.

    Attributes:
        attribute_names: The attributes holding the fields' values - a relation's key attribute for a
            relation.
        description: What declares it, for a message.
        condition: The ``Q`` of a partial uniqueness - the rows it holds for; None for every row.
        nulls_distinct: Whether a row holding NULL in one of the fields is unique by itself.
    """

    attribute_names: tuple[str, ...]
    description: str
    condition: Q | None = None
    nulls_distinct: bool = True

    def get_values(self, obj: Model) -> tuple[Any, ...] | None:
        """The values an obj holds in the fields.

        Args:
            obj: The obj.

        Returns:
            The values; None for values unique by themselves - a NULL among them.
        """
        values = self.read_values(obj)
        if self.nulls_distinct:
            # By identity - `None in values` asks each value's __eq__.
            for value in values:
                if value is None:
                    return None
        return values

    @cached_property
    def read_values(self) -> Callable[[Model], tuple[Any, ...]]:
        """Reads the values an instance holds in the fields, as a tuple - without a Python call per field.

        Returns:
            The reader.
        """
        if len(self.attribute_names) == 1:
            read_value = attrgetter(self.attribute_names[0])
            return lambda instance: (read_value(instance),)
        return attrgetter(*self.attribute_names)

    def holds_for(self, obj: Model) -> bool:
        """Whether the uniqueness holds for a row - a partial one for the rows its condition is true of.

        Args:
            obj: The obj.

        Returns:
            Whether it does; True where the condition can't be read of an obj (a lookup other
            than an equality, a relation path) - the row is then checked.
        """
        return self.condition is None or self.condition_holds(self.condition, obj) is not False

    @classmethod
    def condition_holds(cls, condition: Q, obj: Model) -> bool | None:
        """Whether a condition of equalities of the model's own fields is true of an obj.

        Args:
            condition: The condition.
            obj: The obj.

        Returns:
            Whether it is; None where it can't be read of an obj.
        """
        results: list[bool | None] = [cls.condition_holds(child, obj) for child in condition.children]
        for key, value in condition.filters.items():
            field_name = key.removesuffix(EXACT_LOOKUP_SUFFIX)
            if LOOKUP_SEPARATOR in field_name or not hasattr(obj, field_name):
                results.append(None)
            else:
                results.append(getattr(obj, field_name) == value)
        if condition.connector == Connector.OR:
            holds = True if True in results else None if None in results else False
        else:
            holds = False if False in results else None if None in results else True
        if holds is None or not condition._is_negated:
            return holds
        return not holds

    @classmethod
    def get_declared(cls, model: type[Model]) -> list[DeclaredUniqueness]:
        """The sets of fields a model declares unique.

        Args:
            model: The model.

        Returns:
            Each set - a uniqueness with a condition the database alone can read (``RawSQLTerm``) left
            out.
        """
        meta = model._meta
        declared = [cls(tuple(cls.get_attribute_names(model, meta.primary_key_attribute_names)), "the primary key")]
        for field_name, field in meta.fields_map.items():
            if (
                getattr(field, "unique", False)
                and not getattr(field, "primary_key", False)
                and (
                    field_name in meta.fields_db_projection
                    or field_name in meta.foreign_key_fields | meta.one_to_one_fields
                )
            ):
                declared.append(cls(tuple(cls.get_attribute_names(model, (field_name,))), f"{field_name} (unique)"))
        for constraint in meta.constraints:
            if isinstance(constraint, UniqueConstraint) and constraint.fields:
                if constraint.condition is not None and not isinstance(constraint.condition, Q):
                    continue
                declared.append(
                    cls(
                        tuple(cls.get_attribute_names(model, constraint.fields)),
                        f"the unique constraint {constraint.name or ', '.join(constraint.fields)}",
                        constraint.condition,
                        constraint.nulls_distinct is not False,
                    )
                )
        for index in meta.indexes:
            if isinstance(index, Index) and index.unique and index.fields and not index.expressions:
                condition = getattr(index, "condition", None)
                if condition is not None and not isinstance(condition, Q):
                    continue
                declared.append(
                    cls(
                        tuple(cls.get_attribute_names(model, index.fields)),
                        f"the unique index {index.name or ', '.join(index.fields)}",
                        condition,
                    )
                )
        # A set declared twice - a key field marked unique too - is checked once.
        unique_declared: dict[tuple[tuple[str, ...], Q | None], DeclaredUniqueness] = {}
        for uniqueness in declared:
            unique_declared.setdefault((uniqueness.attribute_names, uniqueness.condition), uniqueness)
        return list(unique_declared.values())

    @staticmethod
    def get_attribute_names(model: type[Model], field_names: Any) -> list[str]:
        """The attributes of an instance holding fields' values.

        Args:
            model: The model.
            field_names: The fields.

        Returns:
            Each field's attribute - a relation's key attributes in its place.
        """
        meta = model._meta
        attribute_names = []
        for field_name in field_names:
            field_name = field_name.lstrip("-")
            field = meta.fields_map[field_name]
            source_fields = getattr(field, "source_fields", ())
            if field_name in meta.foreign_key_fields | meta.one_to_one_fields and source_fields:
                attribute_names.extend(source_fields)
            else:
                attribute_names.append(field_name)
        return attribute_names
