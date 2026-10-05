from __future__ import annotations

from collections.abc import Collection
from typing import TYPE_CHECKING

from hare.fields.encrypted.encrypted_field_base import EncryptedFieldBase
from hare.query.statements.building.query_annotations import QueryAnnotations

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.query.statements.select.values_query import ValuesQuery


class DistinctFirstOccurrence:
    """A distinct() ordered by a field it doesn't select, which keeps the first row of each combination
    of the selected columns instead of deduplicating over the ordering too - and the refusal of
    distinct() over an encrypted field, whose equal values differ in the database, unless the
    primary key is selected."""

    @staticmethod
    def distinct_needs_first_occurrence_rows(query: ValuesQuery, output_field_names: Collection[str]) -> bool:
        """Whether a plain ``.distinct()`` is ordered by a field it doesn't select. ``SELECT DISTINCT``
        would then deduplicate over the ordering columns too - instead the query keeps the first row
        of each combination of the selected columns.

        Args:
            query: The values query.
            output_field_names: The field/annotation names visible to the caller.

        Returns:
            True when the query needs the first-occurrence rows.
        """
        if not query._distinct or query._distinct_on or query._distinct_over_ordering_columns:
            return False
        orderings = query._apply_default_ordering(query._orderings, query._annotations)
        return any(field_name not in output_field_names for field_name, _order in orderings)

    @staticmethod
    def raise_if_distinct_over_encrypted_field(query: ValuesQuery, output_field_names: Collection[str]) -> None:
        """Rejects ``.distinct()`` over a selected encrypted field, unless the primary key is
        selected too (it already makes every row distinct).

        Args:
            query: The values query.
            output_field_names: The field/annotation names actually visible to the caller.

        Raises:
            FieldError: DISTINCT would compare an encrypted field's ciphertext.
        """
        if not query._distinct:
            return
        primary_key_attribute = query.model._meta.primary_key_attribute
        selected_field_names = set(output_field_names)
        if isinstance(primary_key_attribute, tuple):
            if set(primary_key_attribute) <= selected_field_names:
                return
        elif selected_field_names & {primary_key_attribute, "pk"}:
            return
        for field_name in output_field_names:
            EncryptedFieldBase.raise_if_encrypted(
                QueryAnnotations.get_field_object_by_path(query, field_name), "DISTINCT"
            )
