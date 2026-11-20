from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from hare.exceptions import ValidationError
from hare.fields.db_defaults.sql_default import SqlDefault
from hare.inspectdb.constants import JSON_FIELD_PATH

if TYPE_CHECKING:
    from hare.dialects.base.dialect import Dialect
    from hare.fields.field import Field


class InspectedColumnDefaults:
    """Adapt catalog defaults to the fields used by inspectdb and drift."""

    @staticmethod
    def get_for_path(dialect: Dialect, field_path: str, db_default: Any) -> Any:
        """Adapts an introspected column default to the field class it is declared on.

        Args:
            dialect: The dialect the default was read from.
            field_path: Dotted path of the generated field class.
            db_default: The introspected default.

        Returns:
            For a JSONField, a string default parsed as the JSON document it holds (kept as a
            string it would be stored as a JSON string literal) - a JSON null as its text, as None is no
            default; otherwise db_default unchanged.
        """
        if field_path != JSON_FIELD_PATH or not isinstance(db_default, str):
            return db_default
        try:
            document = json.loads(db_default)
        except ValueError:
            return db_default
        if document is None:
            return SqlDefault(dialect.literals.get_string_literal_sql(db_default))
        return document

    @staticmethod
    def get_for_field(dialect: Dialect, field: Field[Any], db_default: Any) -> Any:
        """A literal default as the field takes it - the value it stands for read the way the field reads
        the database's values (``'7'`` an int, ``1`` and ``'t'`` a bool), or its text as a ``SqlDefault``
        when the field takes no value of it (``'NaN'`` of a float, ``'infinity'`` of a date, ``'1 day'`` of a
        duration): a default the field refuses broke ``hare drift`` instead of being compared.

        Args:
            dialect: The dialect the default was read from.
            field: The field, built with the default.
            db_default: The default introspected.

        Returns:
            The default the field takes.
        """
        if type(db_default) not in (str, int, float, bool):
            return db_default
        types = dialect.types
        try:
            types.get_db_value(field, db_default, None)
        except ValidationError:
            pass
        else:
            return db_default
        try:
            read_default = types.get_python_value(field, db_default)
            types.get_db_value(field, read_default, None)
        except (ValidationError, TypeError, ValueError):
            pass
        else:
            return read_default
        if isinstance(db_default, str):
            return SqlDefault(dialect.literals.get_string_literal_sql(db_default))
        return SqlDefault(str(db_default))
