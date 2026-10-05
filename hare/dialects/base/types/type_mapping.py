from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.fields.field import Field
    from hare.sql.terms.term import Term

    #: ``(field, value, instance) -> value``; the field parameter is typed by each converter.
    DbConverter = Callable[..., Any]
    #: ``(field, value) -> value``; the field parameter is typed by each converter.
    PythonConverter = Callable[..., Any]


@dataclass(frozen=True, slots=True)
class TypeMapping:
    """How a dialect stores the columns of one field class; None leaves a value to the field.

    Attributes:
        column_type: The column type, or a ``(field) -> str | None`` building it - None for a field the
            dialect has no column type of.
        generated_sql: The DDL of a database-generated primary key column.
        function_cast: A ``(field, term) -> term`` wrapping the column wherever it is compared,
            ordered or copied.
        to_db: A ``(field, value, instance) -> value`` replacing the field's ``to_db_value()``.
        to_lookup: A ``(field, value, instance) -> value`` replacing the field's
            ``to_lookup_value()``.
        to_python: A ``(field, value) -> value`` replacing the field's ``from_db_value()``.
        json_term: A ``(field, term) -> term`` giving the text a column's value is written into a
            JSON object as - for a value stored in a form JSON can't hold (a UUID in 16 bytes).
        naive_datetime_is_utc: ``to_python`` is the field's own ``from_db_value()`` with a naive
            datetime from the driver read as a UTC instant.
        extension: The database extension the column type needs - created wherever a field of the
            class is used.
        inserted_by_select: Whether an INSERT writing the column writes its rows as a ``SELECT`` of
            them, not as ``VALUES`` - for a value written as an expression the dialect's ``VALUES``
            doesn't read; or a ``(field) -> bool`` telling it of a field.
    """

    column_type: str | Callable[[Field[Any]], str | None] | None = None
    generated_sql: str | None = None
    function_cast: Callable[[Field[Any], Term], Term] | None = None
    to_db: DbConverter | None = None
    to_lookup: DbConverter | None = None
    to_python: PythonConverter | None = None
    json_term: Callable[[Field[Any], Term], Term] | None = None
    naive_datetime_is_utc: bool = False
    extension: str | None = None
    inserted_by_select: bool | Callable[[Field[Any]], bool] = False
