from __future__ import annotations

from collections.abc import Callable
from functools import partial
from typing import TYPE_CHECKING, Any

from hare.dialects.enums import DialectName
from hare.dialects.postgresql.fields.constants import HSTORE_ARRAY_PATH_FUNCTIONS
from hare.exceptions import ValidationError
from hare.fields import Field
from hare.query.enums import Lookup, LookupValueShape

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.models import Model
    from hare.query.filters.lookups.field_lookup import FieldLookup
    from hare.sql.terms.term import Term
from hare.dialects.postgresql.fields.hstore.h_store_text import HStoreText


class HStoreField(Field[dict[str, str | None]]):
    """An ``hstore`` column - a flat map of string keys to string or NULL values, a ``dict`` in Python.
    Needs the ``hstore`` extension; the migration autodetector adds ``CreateExtension("hstore")``
    wherever the field is used. A key or value that isn't a string is written as its ``str()``.
    """

    SUPPORTED_DIALECTS = frozenset({DialectName.POSTGRESQL})

    SQL_TYPE = "hstore"
    field_type = dict
    requires_extension = "hstore"

    def get_lookups(self) -> dict[str, FieldLookup]:
        # Local import: the hstore lookups import this module.
        from hare.dialects.postgresql.lookups.hstore.postgresql_h_store_field_lookups import (
            PostgresqlHStoreFieldLookups,
        )

        return PostgresqlHStoreFieldLookups.get_lookups()

    def get_path_transform(self, segment: str) -> tuple[Callable[[Term], Term], Field[Any]] | None:
        """Every key or value as a text array (``attributes__keys``/``attributes__values``), or
        the value of one key (``attributes__color``) - any segment that isn't one of the
        field's lookups."""
        # Local import: the hstore lookups import this module.
        from hare.dialects.postgresql.lookups.hstore.h_store_value_term import HStoreValueTerm
        from hare.dialects.postgresql.lookups.hstore.postgresql_h_store_field_lookups import (
            PostgresqlHStoreFieldLookups,
        )
        from hare.sql.terms.functions.function import Function

        if segment in HSTORE_ARRAY_PATH_FUNCTIONS:
            return partial(
                Function, HSTORE_ARRAY_PATH_FUNCTIONS[segment]
            ), PostgresqlHStoreFieldLookups.TEXT_ARRAY_FIELD  # type: ignore[arg-type]
        if segment not in PostgresqlHStoreFieldLookups.get_lookup_names():
            return partial(HStoreValueTerm, key=segment), PostgresqlHStoreFieldLookups.VALUE_FIELD  # type: ignore[arg-type]
        return None

    def get_lookup_value_description(self, lookup: str) -> tuple[LookupValueShape, Any] | None:
        """Equality and pair containment take a dict, membership a list of dicts."""
        if lookup in {Lookup.EXACT, Lookup.NOT, Lookup.CONTAINS, Lookup.CONTAINED_BY}:
            return LookupValueShape.VALUE, dict
        if lookup in {Lookup.IN, Lookup.NOT_IN}:
            return LookupValueShape.LIST, dict
        return None

    def get_encoded_mapping(self, value: Any) -> dict[str, str | None]:
        """A mapping as the strings hstore holds.

        Args:
            value: The mapping.

        Returns:
            The mapping of strings to strings or None.

        Raises:
            ValidationError: ``value`` isn't a mapping, or a key or value holds a null byte.
        """
        if not isinstance(value, dict):
            raise ValidationError(f"{self.model_field_name}: expected a dict, got {type(value).__name__}")
        mapping = {str(key): None if item is None else str(item) for key, item in value.items()}
        for key, item in mapping.items():
            if "\x00" in key or (item is not None and "\x00" in item):
                raise ValidationError(f"{self.model_field_name}: an hstore key or value can't hold a null byte")
        return mapping

    def to_db_value(self, value: Any, instance: type[Model] | Model) -> Any:
        if value is None:
            self.validate(value)
            return None
        mapping = self.get_encoded_mapping(value)
        self.validate(mapping)
        return HStoreText.encode(mapping)

    def to_python(self, value: Any) -> Any:
        if value is None or isinstance(value, dict):
            return value
        if not isinstance(value, str):
            # A value assigned in Python - only the database's text form is parsed.
            raise ValidationError(f"{self.model_field_name}: expected a dict, got {type(value).__name__}")
        try:
            return HStoreText.parse(value)
        except ValueError as error:
            raise self.get_validation_error(error, value) from None
