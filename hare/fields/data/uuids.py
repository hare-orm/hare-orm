"""UUIDField."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal, TypeVar, overload
from uuid import UUID, uuid4

from hare.exceptions import ValidationError
from hare.fields.base.field import Field

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.models import Model

TUuid = TypeVar("TUuid", UUID, UUID | None)


class UUIDField(Field[TUuid], UUID):
    """
    UUID Field

    This field can store uuid value.

    If used as a primary key, it will auto-generate a UUID4 by default.
    """

    SQL_TYPE = "CHAR(36)"

    @overload
    def __init__(self: UUIDField[UUID], *, null: Literal[False] = False, **kwargs: Any) -> None: ...

    @overload
    def __init__(self: UUIDField[UUID | None], *, null: Literal[True], **kwargs: Any) -> None: ...

    def __init__(self, **kwargs: Any) -> None:
        # A uuid4 default only when neither default nor db_default is given.
        if (
            (kwargs.get("primary_key") or kwargs.get("pk", False))
            and "default" not in kwargs
            and "db_default" not in kwargs
        ):
            kwargs["default"] = uuid4
        super().__init__(**kwargs)

    def to_db_value(self, value: Any, instance: type[Model] | Model) -> str | None:
        self.validate(value)
        if value is None:
            return None
        if not isinstance(value, UUID):
            # A plain attribute assignment isn't normalized - a malformed string fails here.
            value = self.to_python(value)
        return str(value)

    def to_python(self, value: Any) -> UUID | None:
        if value is None or type(value) is UUID:
            return value
        if isinstance(value, UUID):
            # A driver's own UUID subclass (asyncpg's pgproto.UUID) - a plain uuid.UUID, the
            # same type every other backend and a freshly assigned value have.
            return UUID(int=value.int)
        if not isinstance(value, str):
            # UUID(123) fails with a bare AttributeError, not a ValueError.
            raise ValidationError(
                f"{self.model_field_name}: expected a UUID or UUID string, got {self.get_value_for_message(value)}"
            )
        try:
            return UUID(value)
        except ValueError as exc:
            # UUID(...) raises a bare ValueError for malformed input - wrapped so it fails the
            # same catchable-ValidationError way every other field validation failure does.
            raise ValidationError(f"{self.model_field_name}: {exc}")
