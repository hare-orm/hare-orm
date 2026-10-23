from __future__ import annotations

from collections.abc import Collection
from typing import TYPE_CHECKING

from hare.fields.encrypted.encrypted_field_base import EncryptedFieldBase
from hare.query.statements.building.query_annotations import QueryAnnotations

if TYPE_CHECKING:
    from hare.query.statements.select.values_query import ValuesQuery


class ValuesDistinct:
    """A plain ``.distinct()`` of ``values()`` rows over an encrypted field, whose equal values differ in the
    database - refused unless the primary key is selected."""

    @staticmethod
    def raise_if_over_encrypted_field(query: ValuesQuery, output_field_names: Collection[str]) -> None:
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
