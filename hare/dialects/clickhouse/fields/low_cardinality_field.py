from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from hare.dialects.clickhouse.enums import ClickhouseDialectName
from hare.exceptions import ConfigurationError
from hare.fields.data.containers.container_field import ContainerField
from hare.fields.field import Field

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.fields.narrowing.narrowing_limit import NarrowingLimit
    from hare.models import Model
    from hare.query.filters.lookups.field_lookup import FieldLookup
    from hare.sql.terms.term import Term


class LowCardinalityField(Field[Any]):
    """``LowCardinality(T)`` - the values of ``base_field`` stored as a dictionary of the distinct ones,
    for a column with few of them (a country, a status)::

        country = LowCardinalityField(fields.CharField(max_length=2))

    Its values, lookups and paths are those of ``base_field``; ``null=True`` on it is
    ``LowCardinality(Nullable(T))``.

    Args:
        base_field: The field of the values - not a container.

    Raises:
        ConfigurationError: ``base_field`` isn't a field instance, or is a container.
    """

    SUPPORTED_DIALECTS = frozenset({ClickhouseDialectName.CLICKHOUSE})
    keeps_native_db_values = False

    def __init__(self, base_field: Field[Any], **kwargs: Any) -> None:
        if not isinstance(base_field, Field) or isinstance(base_field, ContainerField):
            raise ConfigurationError(
                "LowCardinalityField(base_field=...) takes a field instance that isn't a container, got "
                f"{base_field!r}"
            )
        self.base_field = base_field
        self.field_type = base_field.field_type
        self.enum_type = base_field.enum_type
        super().__init__(**kwargs)

    @property
    def constraints(self) -> dict[str, Any]:
        return self.base_field.constraints

    def to_db_value(self, value: Any, instance: type[Model] | Model) -> Any:
        self.validate(value)
        return None if value is None else self.base_field.to_db_value(value, instance)

    def to_python(self, value: Any) -> Any:
        return None if value is None else self.base_field.to_python(value)

    def from_db_value(self, value: Any) -> Any:
        return None if value is None else self.base_field.from_db_value(value)

    def get_lookups(self) -> dict[str, FieldLookup]:
        return self.base_field.get_lookups()

    def get_path_transform(self, segment: str) -> tuple[Callable[[Term], Term], Field[Any]] | None:
        return self.base_field.get_path_transform(segment)

    def get_narrowing_limit(self, old_field: Field[Any]) -> NarrowingLimit | None:
        old_base_field = old_field.base_field if isinstance(old_field, LowCardinalityField) else old_field
        return self.base_field.get_narrowing_limit(old_base_field)

    def get_python_type(self) -> Any:
        return self.base_field.get_python_type()
