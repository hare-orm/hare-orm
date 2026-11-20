from __future__ import annotations

from collections.abc import Callable
from functools import partial
from typing import TYPE_CHECKING

from hare.fields.data.text.string_field import StringField

if TYPE_CHECKING:
    from hare.sql.terms.term import Term


class CitextField(StringField[str]):
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

    def get_assign_normalized_types(self) -> frozenset[type]:
        return self._get_unchanged_assign_types(CitextField.to_python, self.field_type)
