from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from hare.ddl.conditions.field_condition import FieldCondition
from hare.ddl.constraints.check_constraint import CheckConstraint
from hare.ddl.constraints.exclusion_constraint import ExclusionConstraint
from hare.ddl.constraints.unique_constraint import UniqueConstraint
from hare.ddl.expressions.expression_field_renaming import ExpressionFieldRenaming
from hare.exceptions import ConfigurationError

if TYPE_CHECKING:
    from hare.ddl.indexes.index import Index
    from hare.fields.field import Field


class SchemaReferences:
    """Read local field dependencies from declarations without interpreting raw SQL.

    Unknown declarations and opaque SQL provide no inferred references. Their author
    must order explicit removal and replacement operations when a field changes.
    """

    @staticmethod
    def get_field_attribute_names(
        field_name: str, field: Any, *, target_key_names: tuple[str, ...] | None = None
    ) -> frozenset[str]:
        """Logical attributes provided by a declaration, including relation key columns.

        Migration snapshots intentionally carry no live relation bindings. The frozen
        target declaration supplies the same shadow names as normal relation linking;
        a physical ``source_field`` override does not rename these Python attributes.
        """
        from hare.core.apps.relation_linking import RelationLinking
        from hare.fields.relations.fields.foreign_key_field_instance import ForeignKeyFieldInstance

        names = {field_name}
        if isinstance(field, ForeignKeyFieldInstance):
            target = field.to_field
            targets = (target,) if isinstance(target, str) else tuple(target) if target else target_key_names
            if targets is None:
                raise ConfigurationError("A relation with implicit target keys needs its captured target declaration")
            names.update(RelationLinking.get_key_field_names(field_name, targets))
        return frozenset(names)

    @classmethod
    def get_dependent_field_names(
        cls,
        fields: Mapping[str, Any],
        field_name: str,
        *,
        field_attributes: Mapping[str, frozenset[str]] | None = None,
    ) -> frozenset[str]:
        """A field and the transitive structured generated columns reading its attributes.

        Relation shadow keys participate without live model bindings. This is the
        declaration closure needed both when deferring creation and before removal.
        """
        selected = {field_name}
        attributes: set[str] = set()
        while True:
            for name in selected:
                attributes.update(
                    field_attributes[name]
                    if field_attributes is not None
                    else cls.get_field_attribute_names(name, fields[name])
                )
            dependent = {
                name
                for name, field in fields.items()
                if name not in selected and not attributes.isdisjoint(field.get_generated_from_field_names())
            }
            if not dependent:
                return frozenset(selected)
            selected.update(dependent)

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
