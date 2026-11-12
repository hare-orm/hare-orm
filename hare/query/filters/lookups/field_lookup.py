from __future__ import annotations

import dataclasses
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from hare.dialects.base.dialect import Dialect
    from hare.fields.field import Field
    from hare.sql.terms.term import Term


@dataclasses.dataclass(frozen=True, slots=True)
class FieldLookup:
    """One ``__<lookup>`` of a value: the criterion it builds and how it encodes the filter value.

    Attributes:
        operator: Builds the criterion - ``(term, encoded_value) -> Criterion``.
        value_encoder: Encodes the filter value - ``(value, model, field, dialect)``; None to
            convert it as the field converts its values.
        value_encoder_builder: Optional ``(field, dialect) -> encoder`` preparing fixed conversions
            while preserving the ordinary encoder's per-value checks and invocation arguments.
        array_element_field: The field each item of a list value is bound as.
        array_element_fields: Per key column, the field each item of a list of key rows is
            bound as.
        text_function: Turns the value into the text a text lookup matches.
        compares_json_path_text: Whether the lookup matches the text of the value at a JSON path.
        is_tsvector: Whether a ``__search`` matches a stored text search vector as it is.
        search_config: The text search configuration of that vector.
        requires_search_query_config: Reject an implicit query configuration for a computed vector
            whose declaration has no common configuration.
        searched_field: The field a ``__search`` matches - a dialect searching through a
            full-text index finds the field's index by it.
        required_feature: The ``Features`` flag a connection needs to run the lookup; the query
            raises ``UnSupportedError`` before any SQL on a connection without it.
    """

    operator: Callable[..., Any]
    value_encoder: Callable[..., Any] | None = None
    array_element_field: Field[Any] | None = None
    array_element_fields: tuple[Field[Any] | None, ...] | None = None
    text_function: Callable[[Term], Term] | None = None
    compares_json_path_text: bool = False
    is_tsvector: bool = False
    search_config: str | None = None
    requires_search_query_config: bool = False
    searched_field: Field[Any] | None = None
    required_feature: str | None = None
    value_encoder_builder: Callable[[Field[Any], Dialect], Callable[..., Any]] | None = None

    def get_value_encoder(self, field: Field[Any], dialect: Dialect) -> Callable[..., Any] | None:
        """Prepare an opted-in encoder once for a query's field and dialect.

        Plain extension callbacks are returned unchanged. The builder retains only conversion
        metadata; the model and each value continue to arrive at invocation time.
        """
        return self.value_encoder if self.value_encoder_builder is None else self.value_encoder_builder(field, dialect)

    def with_changes(self, **changes: Any) -> FieldLookup:
        """A copy with some attributes replaced.

        Args:
            changes: The attributes to replace.

        Returns:
            The copy.
        """
        return dataclasses.replace(self, **changes)
