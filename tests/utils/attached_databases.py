"""The test database URL of models with ``Meta.schema``: on SQLite a schema is an attached database."""

from collections.abc import Iterable


def get_url_with_attached_databases(db_url: str, schemas: Iterable[str]) -> str:
    """The URL with an in-memory database attached under each schema's name on SQLite; any other URL
    as it is.

    Args:
        db_url: The test database URL.
        schemas: The schemas of the models.

    Returns:
        The URL.
    """
    if not db_url.startswith("sqlite"):
        return db_url
    attached = ",".join(f"{schema}::memory:" for schema in schemas)
    separator = "&" if "?" in db_url else "?"
    return f"{db_url}{separator}attached_databases={attached}"
