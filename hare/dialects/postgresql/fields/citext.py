from __future__ import annotations

from collections.abc import Callable
from functools import partial
from typing import TYPE_CHECKING, Any

from hare.dialects.enums import DialectName
from hare.exceptions import ValidationError
from hare.fields import Field
from hare.fields.constants import NULL_BYTE_MESSAGE

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.models import Model
    from hare.sql.terms.base.term import Term


class CitextField(Field[str]):
    """A ``CITEXT`` column - text compared, indexed and sorted ignoring case. Needs the ``citext``
    extension; the migration autodetector adds ``CreateExtension("citext")`` wherever the field is
    used.
    """

    SUPPORTED_DIALECTS = frozenset({DialectName.POSTGRESQL})

    SQL_TYPE = "CITEXT"
    field_type = str
    requires_extension = "citext"

    def get_like_text_function(self) -> Callable[[Term], Term] | None:
        # Kept citext, not cast to VARCHAR - citext's LIKE ignores case, as its equality does.
        # Local import: the SQL functions import the fields package.
        from hare.sql.functions.cast import Cast

        return partial(Cast, as_type=self.SQL_TYPE)

    def to_db_value(self, value: Any, instance: type[Model] | Model) -> Any:
        value = super().to_db_value(value, instance)
        # Postgres's text protocol can't carry a null byte in any text type - rejected upfront
        # with the same ValidationError as CharField/TextField instead of a raw driver error.
        if isinstance(value, str) and "\x00" in value:
            raise ValidationError(f"{self.model_field_name}: {NULL_BYTE_MESSAGE}")
        return value
