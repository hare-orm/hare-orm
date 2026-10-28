from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal, TypeVar, overload
from uuid import UUID

from hare.exceptions import ValidationError
from hare.fields.data.constants import UUID_VERSION_DEFAULTS
from hare.fields.field import Field
from hare.fields.validators.formats.uuid_version_validator import UUIDVersionValidator

if TYPE_CHECKING:
    from hare.models import Model

TUuid = TypeVar("TUuid", UUID, UUID | None)


class UUIDField(Field[TUuid]):
    """A UUID - a ``CHAR(36)`` column, or the database's own UUID type. As a primary key without a
    ``default`` or ``db_default`` it defaults to ``uuid4()`` - to ``Uuid7.new()`` with ``version=7``.

    Args:
        version: The version of RFC 9562 every value is of - another one is refused on every write and in
            a filter (``ValidationError``); any version when None.
    """

    field_type = UUID

    SQL_TYPE = "CHAR(36)"

    @overload
    def __init__(
        self: UUIDField[UUID], *, null: Literal[False] = False, version: int | None = None, **kwargs: Any
    ) -> None: ...

    @overload
    def __init__(
        self: UUIDField[UUID | None], *, null: Literal[True], version: int | None = None, **kwargs: Any
    ) -> None: ...

    def __init__(self, *, version: int | None = None, **kwargs: Any) -> None:
        # A default only when neither default nor db_default is given - of the field's version.
        if (
            (kwargs.get("primary_key") or kwargs.get("pk", False))
            and "default" not in kwargs
            and "db_default" not in kwargs
            and version in UUID_VERSION_DEFAULTS
        ):
            kwargs["default"] = UUID_VERSION_DEFAULTS[version]
        super().__init__(**kwargs)
        self.version = version
        if version is not None:
            self.validators = [*self.validators, UUIDVersionValidator(version)]

    def to_db_value(self, value: Any, instance: type[Model] | Model | None) -> str | None:
        if value is not None and not isinstance(value, UUID):
            # A plain attribute assignment or a filter value isn't normalized - a malformed string fails
            # here, and the validators get the UUID.
            value = self.to_python(value)
        self.validate(value)
        if value is None:
            return None
        return str(value)

    def to_db_uuid(self, value: Any, instance: type[Model] | Model) -> UUID | None:
        """The value written as the UUID itself - for a dialect whose driver writes one, where
        ``to_db_value()`` gives its text.

        Args:
            value: The value.
            instance: The instance written, or its model.

        Returns:
            The UUID, or None.
        """
        if value is not None and type(value) is not UUID:
            # A plain attribute assignment or a filter value isn't normalized - a malformed string fails
            # here, and the validators get the UUID.
            value = self.to_python(value)
        self.validate(value)
        return value

    def to_python(self, value: Any) -> UUID | None:
        if value is None or type(value) is UUID:
            return value
        if isinstance(value, UUID):
            # A driver's own UUID subclass - a plain uuid.UUID, the
            # same type every other backend and a freshly assigned value have.
            return UUID(int=value.int)
        if not isinstance(value, str):
            # UUID(123) fails with a bare AttributeError, not a ValueError.
            raise ValidationError(
                f"{self.model_field_name}: expected a UUID or UUID string, got {self.get_value_for_message(value)}"
            )
        try:
            return UUID(value)
        except ValueError as error:
            # UUID(...) raises a bare ValueError for malformed input - wrapped so it fails the
            # same catchable-ValidationError way every other field validation failure does.
            raise ValidationError(f"{self.model_field_name}: {error}")
