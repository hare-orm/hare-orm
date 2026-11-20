from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class StoredValueSnapshot:
    """Compiled staging statements retaining original row identities during a key rewrite."""

    create_sql: str
    select_sql: str
    drop_sql: str
