from __future__ import annotations

from collections.abc import Callable
from functools import partial
from typing import TYPE_CHECKING, Any, ClassVar

from hare.dialects.enums import DialectName
from hare.dialects.postgresql.fields.constants import POSTGRESQL_NETWORK_PATH_FUNCTIONS
from hare.fields.data.numeric.int_field import IntField
from hare.fields.field import Field

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.models import Model
    from hare.query.filters.lookups.field_lookup import FieldLookup
    from hare.sql.terms.term import Term


class NetworkField(Field[Any]):
    """Base of the PostgreSQL address and network fields (``inet``, ``cidr``): a value is parsed by
    ``parse()`` - text or an ``ipaddress`` object - and written as its text. Besides equality and
    comparison they filter by subnet (``__net_contained``, ``__net_contains``, ...) and read their
    family and prefix length as paths (``ip__family``, ``ip__masklen``).
    """

    SUPPORTED_DIALECTS = frozenset({DialectName.POSTGRESQL})

    #: The field of a path read from the value - an int for both.
    PATH_FIELD: ClassVar[IntField[Any]] = IntField()

    def parse(self, value: Any) -> Any:
        """The ``ipaddress`` object of a value.

        Raises:
            ValueError: The value isn't of the field's type.
        """
        raise NotImplementedError

    def get_parsed(self, value: Any) -> Any:
        """The ``ipaddress`` object of an address or a network.

        Args:
            value: The value - text or an ``ipaddress`` object.

        Returns:
            The object.

        Raises:
            ValidationError: The value isn't one of the field's type.
        """
        try:
            return self.parse(value)
        except (ValueError, TypeError) as error:
            raise self.get_validation_error(error, value) from error

    def to_python(self, value: Any) -> Any:
        return None if value is None else self.get_parsed(value)

    def to_db_value(self, value: Any, instance: type[Model] | Model) -> Any:
        if value is None:
            self.validate(value)
            return None
        parsed = self.get_parsed(value)
        self.validate(parsed)
        return str(parsed)

    def get_lookups(self) -> dict[str, FieldLookup]:
        # Local import: the network lookups import the dialect package this module is part of.
        from hare.dialects.postgresql.lookups.network.postgresql_network_field_lookups import (
            PostgresqlNetworkFieldLookups,
        )

        return PostgresqlNetworkFieldLookups.get_lookups(self)

    def get_path_transform(self, segment: str) -> tuple[Callable[[Term], Term], Field[Any]] | None:
        """The family (``ip__family``, 4 or 6) or the prefix length (``ip__masklen``) of the value."""
        # Local import: the SQL terms import the fields package.
        from hare.sql.terms.functions.function import Function

        if segment in POSTGRESQL_NETWORK_PATH_FUNCTIONS:
            return partial(Function, POSTGRESQL_NETWORK_PATH_FUNCTIONS[segment]), self.PATH_FIELD  # type: ignore[call-overload]
        return super().get_path_transform(segment)
