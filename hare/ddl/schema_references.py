from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hare.ddl.conditions.field_condition import FieldCondition
from hare.ddl.constraints.check_constraint import CheckConstraint
from hare.ddl.constraints.exclusion_constraint import ExclusionConstraint
from hare.ddl.constraints.unique_constraint import UniqueConstraint
from hare.ddl.expressions.expression_field_renaming import ExpressionFieldRenaming

if TYPE_CHECKING:
    from hare.ddl.indexes.index import Index
    from hare.fields.field import Field


class SchemaReferences:
    """Read local field dependencies from declarations without interpreting raw SQL.

    Unknown declarations and opaque SQL provide no inferred references. Their author
    must order explicit removal and replacement operations when a field changes.
    """

    @staticmethod
    def get_index_field_names(index: Index) -> frozenset[str]:
        """Keys, included columns and structured conditions read by an index."""
        names = set(index.fields) | set(index.include)
        names.update(ExpressionFieldRenaming.get_all_read_field_names(index.declared_expressions))
        condition = getattr(index, "condition", None)
        if isinstance(condition, FieldCondition):
            names.update(condition.get_referenced_field_names())
        return frozenset(names)

    @staticmethod
    def get_constraint_field_names(constraint: Any) -> frozenset[str]:
        """Declared keys and structured predicates read by a supported constraint."""
        names: set[str] = set()
        condition = None
        if isinstance(constraint, CheckConstraint):
            condition = constraint.check
        elif isinstance(constraint, UniqueConstraint):
            names.update(constraint.fields)
            names.update(constraint.include)
            condition = constraint.condition
        elif isinstance(constraint, ExclusionConstraint):
            names.update(constraint.get_read_field_names())
            names.update(constraint.include)
            condition = constraint.condition
        if isinstance(condition, FieldCondition):
            names.update(condition.get_referenced_field_names())
        return frozenset(names)

    @staticmethod
    def references_field(names: frozenset[str], field: Field[Any]) -> bool:
        """Match a logical field or one of its declared relation key attributes."""
        return not names.isdisjoint({field.model_field_name, *getattr(field, "source_fields", ())})
