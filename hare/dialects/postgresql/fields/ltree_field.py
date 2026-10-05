from __future__ import annotations

from collections.abc import Callable
from functools import partial
from typing import TYPE_CHECKING, Any, ClassVar

from hare.dialects.enums import DialectName
from hare.dialects.postgresql.fields.constants import (
    LTREE_LABEL_PATTERN,
    LTREE_LABEL_SEPARATOR,
    POSTGRESQL_LTREE_PATH_FUNCTIONS,
    POSTGRESQL_LTREE_TYPE,
)
from hare.exceptions import ValidationError
from hare.fields.data.numeric.int_field import IntField
from hare.fields.field import Field

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.models import Model
    from hare.query.filters.lookups.field_lookup import FieldLookup
    from hare.sql.terms.term import Term


class LtreeField(Field[str]):
    """An ``ltree`` column - a label path in a tree (``Top.Science.Astronomy``), a ``str`` in Python.
    Needs the ``ltree`` extension; the migration autodetector adds ``CreateExtension("ltree")``
    wherever the field is used. A list or tuple of labels is taken as the path they make. Besides
    equality and ordering it filters by the tree (``__ancestor_of``, ``__descendant_of``) and by
    ``lquery``/``ltxtquery`` patterns, and reads its number of labels as ``path__depth``.
    """

    SUPPORTED_DIALECTS = frozenset({DialectName.POSTGRESQL})

    SQL_TYPE = POSTGRESQL_LTREE_TYPE
    field_type = str
    requires_extension = "ltree"

    #: The field of ``path__depth`` - an int.
    DEPTH_FIELD: ClassVar[IntField[Any]] = IntField()

    def get_path_text(self, value: Any) -> str:
        """The text of a path - a ``str``, or a list or tuple of labels.

        Args:
            value: The path.

        Returns:
            The labels joined by dots; ``""`` is the empty path.

        Raises:
            ValidationError: ``value`` is of another type, or a label is empty or holds a character
                other than a letter, a digit, ``_`` or ``-``.
        """
        if isinstance(value, (list, tuple)):
            labels = list(value)
            if not all(isinstance(label, str) for label in labels):
                raise ValidationError(
                    f"{self.model_field_name}: every label must be a str, got {self.get_value_for_message(value)}"
                )
        elif isinstance(value, str):
            labels = value.split(LTREE_LABEL_SEPARATOR) if value else []
        else:
            raise ValidationError(
                f"{self.model_field_name}: expected a str or a list of labels, got {self.get_value_for_message(value)}"
            )
        for label in labels:
            if LTREE_LABEL_PATTERN.fullmatch(label) is None:
                raise ValidationError(
                    f"{self.model_field_name}: {self.get_value_for_message(value)} is not an ltree path - a label "
                    "holds only letters, digits, underscores and hyphens and is not empty"
                )
        return LTREE_LABEL_SEPARATOR.join(labels)

    def to_python(self, value: Any) -> str | None:
        if isinstance(value, (list, tuple)):
            return self.get_path_text(value)
        return value

    def to_db_value(self, value: Any, instance: type[Model] | Model) -> Any:
        if value is None:
            self.validate(value)
            return None
        path_text = self.get_path_text(value)
        self.validate(path_text)
        return path_text

    def get_lookups(self) -> dict[str, FieldLookup]:
        # Local import: the ltree lookups import the dialect package this module is part of.
        from hare.dialects.postgresql.lookups.ltree.postgresql_ltree_field_lookups import PostgresqlLtreeFieldLookups

        return PostgresqlLtreeFieldLookups.get_lookups(self)

    def get_path_transform(self, segment: str) -> tuple[Callable[[Term], Term], Field[Any]] | None:
        """The number of labels of the path (``path__depth``)."""
        # Local import: the SQL terms import the fields package.
        from hare.sql.terms.functions.function import Function

        if segment in POSTGRESQL_LTREE_PATH_FUNCTIONS:
            return partial(Function, POSTGRESQL_LTREE_PATH_FUNCTIONS[segment]), self.DEPTH_FIELD  # type: ignore[call-overload]
        return None
