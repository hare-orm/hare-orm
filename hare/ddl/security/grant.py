from __future__ import annotations

from dataclasses import dataclass
from typing import Any, cast

from hare.classes.class_path import ClassPath
from hare.ddl.constants import COLUMN_PRIVILEGES, GRANT_TARGET_PRIVILEGES
from hare.ddl.enums import GrantTarget, Privilege
from hare.exceptions import ConfigurationError


@dataclass(frozen=True)
class Grant:
    """Privileges a model declares in ``Meta.grants`` for roles - on its table, or on a view,
    materialized view, sequence or function the model declares. Removing it revokes them.

    Args:
        privileges: The privileges - ``Privilege``.
        roles: The roles that get them (``"PUBLIC"`` for every role).
        on: The type of object - ``GrantTarget``; the model's table by default.
        object_name: The name of the object of the model's ``Meta`` - for every type but the table.
        columns: Fields of the model the privileges are limited to - on the table only, for
            ``SELECT``, ``INSERT``, ``UPDATE`` and ``REFERENCES``.
        with_grant_option: The roles may grant the privileges on.

    Raises:
        ConfigurationError: The privileges or roles are empty or of the wrong type, a privilege
            doesn't exist on the type of object, ``object_name`` is missing or given for the table,
            or ``columns`` are given where they can't be.
    """

    privileges: tuple[Privilege, ...]
    roles: tuple[str, ...]
    on: GrantTarget = GrantTarget.TABLE
    object_name: str | None = None
    columns: tuple[str, ...] = ()
    with_grant_option: bool = False

    def __post_init__(self) -> None:
        if self.on not in set(GrantTarget):
            raise ConfigurationError(f"Grant.on must be one of {', '.join(GrantTarget)}, got {self.on!r}")
        if isinstance(cast("object", self.privileges), str) or not self.privileges:
            raise ConfigurationError(f"Grant.privileges must be a non-empty sequence, got {self.privileges!r}")
        allowed_privileges = GRANT_TARGET_PRIVILEGES[GrantTarget(self.on)]
        for privilege in self.privileges:
            if privilege not in allowed_privileges:
                raise ConfigurationError(
                    f"Grant: a {GrantTarget(self.on)} has no '{privilege}' privilege - "
                    f"one of {', '.join(sorted(allowed_privileges))}"
                )
        object.__setattr__(self, "privileges", tuple(Privilege(privilege) for privilege in self.privileges))
        if len(set(self.privileges)) != len(self.privileges):
            raise ConfigurationError(f"Grant.privileges names a privilege twice: {self.privileges!r}")
        if (
            isinstance(cast("object", self.roles), str)
            or not self.roles
            or not all(isinstance(role, str) and role for role in self.roles)
        ):
            raise ConfigurationError(f"Grant.roles must be a non-empty sequence of role names, got {self.roles!r}")
        object.__setattr__(self, "roles", tuple(self.roles))
        if self.on == GrantTarget.TABLE:
            if self.object_name is not None:
                raise ConfigurationError("Grant.object_name names a view, sequence or function - not the table")
        elif not isinstance(self.object_name, str) or not self.object_name:
            raise ConfigurationError(f"Grant on a {self.on} needs the object_name of it")
        if isinstance(cast("object", self.columns), str) or not all(
            isinstance(column, str) and column for column in self.columns
        ):
            raise ConfigurationError(f"Grant.columns must be a sequence of field names, got {self.columns!r}")
        object.__setattr__(self, "columns", tuple(self.columns))
        if self.columns:
            if self.on != GrantTarget.TABLE:
                raise ConfigurationError("Grant.columns limits a grant on the table only")
            if not set(self.privileges) <= COLUMN_PRIVILEGES:
                raise ConfigurationError(
                    f"Grant.columns limits only the {', '.join(sorted(COLUMN_PRIVILEGES))} privileges, "
                    f"got {self.privileges!r}"
                )
        if not isinstance(self.with_grant_option, bool):
            raise ConfigurationError(f"Grant.with_grant_option must be a bool, got {self.with_grant_option!r}")

    @property
    def name(self) -> None:
        """A grant has no name - it is known by everything it grants."""
        return None

    def describe(self) -> str:
        """The grant in words, for messages."""
        target = "table" if self.on == GrantTarget.TABLE else f"{self.on} {self.object_name}"
        columns = f" ({', '.join(self.columns)})" if self.columns else ""
        return f"{', '.join(self.privileges)}{columns} on {target} to {', '.join(self.roles)}"

    def deconstruct(self) -> tuple[str, list[Any], dict[str, Any]]:
        kwargs: dict[str, Any] = {"privileges": self.privileges, "roles": self.roles}
        if self.on != GrantTarget.TABLE:
            kwargs["on"] = GrantTarget(self.on)
            kwargs["object_name"] = self.object_name
        if self.columns:
            kwargs["columns"] = self.columns
        if self.with_grant_option:
            kwargs["with_grant_option"] = True
        return ClassPath.get(type(self)), [], kwargs
