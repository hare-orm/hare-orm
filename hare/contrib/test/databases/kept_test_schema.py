from __future__ import annotations

import json
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class KeptTestSchema:
    """The schema a reusable test database keeps from one lease to the next, its rows cleared."""

    #: The digest of the DDL that created the schema.
    schema_key: str
    #: A transaction id taken once the schema stood as created and its tables were empty - a catalog
    #: row written by a later transaction means the schema changed since.
    catalog_transaction_id: int
    #: The number of rows of every catalog then, by catalog - DDL that only deletes rows writes none.
    catalog_row_counts: tuple[tuple[str, int], ...]
    #: The digest of every relation's definition then - its catalog is written by statements changing
    #: none too (TRUNCATE gives a table new storage).
    class_definitions_fingerprint: str

    def to_text(self) -> str:
        """The schema as the text a database's comment keeps it in.

        Returns:
            The text.
        """
        return json.dumps(
            [self.schema_key, self.catalog_transaction_id, self.catalog_row_counts, self.class_definitions_fingerprint]
        )

    @classmethod
    def from_text(cls, text: str) -> KeptTestSchema | None:
        """The schema a database's comment keeps.

        Args:
            text: The text ``to_text()`` wrote.

        Returns:
            The schema, or None when the text holds none.
        """
        try:
            schema_key, catalog_transaction_id, catalog_row_counts, class_definitions_fingerprint = json.loads(text)
            return cls(
                str(schema_key),
                int(catalog_transaction_id),
                tuple((str(catalog_name), int(row_count)) for catalog_name, row_count in catalog_row_counts),
                str(class_definitions_fingerprint),
            )
        except (TypeError, ValueError):
            return None
