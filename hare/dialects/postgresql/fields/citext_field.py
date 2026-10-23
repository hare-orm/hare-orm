from __future__ import annotations

from collections.abc import Callable
from functools import partial
from typing import TYPE_CHECKING, Any

from hare.fields import Field
from hare.fields.data.text.text_values import TextValues

if TYPE_CHECKING:
    from hare.sql.terms.term import Term


class CitextField(Field[str]):
    """A ``CITEXT`` column - text compared, indexed and sorted ignoring case. Needs the ``citext``
    extension; the migration autodetector adds ``CreateExtension("citext")`` wherever the field is
    used.
    """

    COLUMN_TYPE_FROM_DIALECT = True

    SQL_TYPE = "CITEXT"
    field_type = str

    def get_like_text_function(self) -> Callable[[Term], Term] | None:
        # Kept citext, not cast to VARCHAR - citext's LIKE ignores case, as its equality does.
        # Local import: the SQL functions import the fields package.
        from hare.sql.functions.cast import Cast

        return partial(Cast, as_type=self.SQL_TYPE)

    def get_written_value_of_another_type(self, value: Any) -> Any:
        TextValues.raise_if_not_text(self, value)
        return super().get_written_value_of_another_type(value)

    def to_python(self, value: Any) -> Any:
        if value is not None and not isinstance(value, str):
            TextValues.raise_if_not_text(self, value)
        return super().to_python(value)

    def from_db_value(self, value: Any) -> Any:
        return self.to_python(TextValues.get_read_text(value))
