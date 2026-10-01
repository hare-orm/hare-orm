from __future__ import annotations

from typing import TYPE_CHECKING, Any, TypeVar

from hare.fields.enums import RelationType

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.models import Model
    from hare.query.filters.field_lookup import FieldLookup
from hare.fields.relations.fields.relational_field import RelationalField

TModel = TypeVar("TModel", bound="Model")


class BackwardFKRelation(RelationalField[TModel]):
    relation_type = RelationType.BACKWARD_FOREIGN_KEY
    is_multi_valued = True

    def __init__(
        self,
        field_type: type[TModel],
        relation_field: str,
        relation_source_field: str,
        null: bool,
        description: str | None,
        relation_fields: tuple[str, ...] | None = None,
        relation_source_fields: tuple[str, ...] | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(field_type, null=null, **kwargs)
        self.relation_field: str = relation_field  # UNCHANGED: primary/first shadow column name
        self.relation_source_field: str = relation_source_field  # UNCHANGED: primary/first shadow column source_field
        self.relation_fields: tuple[str, ...] = relation_fields or (relation_field,)
        self.relation_source_fields: tuple[str, ...] = relation_source_fields or (relation_source_field,)
        self.description: str | None = description

    def get_lookups(self) -> dict[str, FieldLookup]:
        """The lookups of a backward relation itself - its rows' keys, and ``isnull``."""
        # Local import: the filters package imports the fields package.
        from hare.query.filters.field_lookups import FieldLookups

        return FieldLookups.get_backward(self)
