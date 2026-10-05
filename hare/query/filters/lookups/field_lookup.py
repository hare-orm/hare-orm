from __future__ import annotations

import dataclasses
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.fields.field import Field
    from hare.sql.terms.term import Term


@dataclasses.dataclass(frozen=True, slots=True)
class FieldLookup:
    """One ``__<lookup>`` of a value: the criterion it builds and how it encodes the filter value.

    Attributes:
        operator: Builds the criterion - ``(term, encoded_value) -> Criterion``.
        value_encoder: Encodes the filter value - ``(value, model, field, dialect)``; None to
            convert it as the field converts its values.
        array_element_field: The field each item of a list value is bound as.
        array_element_fields: Per key column, the field each item of a list of key rows is
            bound as.
        text_function: Turns the value into the text a text lookup matches.
        compares_json_path_text: Whether the lookup matches the text of the value at a JSON path.
        is_tsvector: Whether a ``__search`` matches a stored text search vector as it is.
        search_config: The text search configuration of that vector.
        searched_field: The field a ``__search`` matches - a dialect searching through a
            full-text index finds the field's index by it.
        required_feature: The ``Features`` flag a connection needs to run the lookup; the query
            raises ``UnSupportedError`` before any SQL on a connection without it.
        binds_by_rebuild: Whether a query plan binds a later value by building the criterion
            again from it - for an operator that derives what it compares from the value (a JSON
            ``__filter``, a transformed value). The SQL text it builds must depend on nothing of
            the value but its type and a list's length - the plan key holds those (and a JSON
            ``__filter`` dict's keys).
    """

    operator: Callable[..., Any]
    value_encoder: Callable[..., Any] | None = None
    array_element_field: Field[Any] | None = None
    array_element_fields: tuple[Field[Any] | None, ...] | None = None
    text_function: Callable[[Term], Term] | None = None
    compares_json_path_text: bool = False
    is_tsvector: bool = False
    search_config: str | None = None
    searched_field: Field[Any] | None = None
    required_feature: str | None = None
    binds_by_rebuild: bool = False

    def with_changes(self, **changes: Any) -> FieldLookup:
        """A copy with some attributes replaced.

        Args:
            changes: The attributes to replace.

        Returns:
            The copy.
        """
        return dataclasses.replace(self, **changes)
