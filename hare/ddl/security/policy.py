from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from hare.classes.class_path import ClassPath
from hare.ddl.conditions.constraint_condition import ConstraintCondition
from hare.ddl.conditions.tenant_condition import TenantCondition
from hare.ddl.constants import POLICY_COMMANDS_WITHOUT_USING, POLICY_COMMANDS_WITHOUT_WITH_CHECK
from hare.ddl.enums import PolicyCommand
from hare.ddl.schema_objects.named_schema_object import NamedSchemaObject
from hare.exceptions import ConfigurationError

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.ddl.raw_sql_term import RawSQLTerm
    from hare.query.expressions import Q


@dataclass(frozen=True)
class Policy(NamedSchemaObject):
    """A row level security policy a model declares in ``Meta.policies`` - which rows of its table a
    role reads and writes once ``Meta.row_level_security`` is on.

    Args:
        name: The policy's name - per table.
        command: The statements it applies to - ``PolicyCommand``.
        roles: The roles it applies to; empty for every role.
        using: The condition a row a statement reads or changes has to meet - a ``Q`` over the
            model's own fields, ``RawSQLTerm``, or ``TenantCondition()`` (the row's tenant is one of
            the transaction's). None for an ``INSERT`` policy.
        with_check: The condition a row a statement writes has to meet. None for a ``SELECT`` or
            ``DELETE`` policy; without it an ``ALL``/``UPDATE`` policy checks ``using``.
        permissive: True - a row passing any permissive policy passes; False - a restrictive
            policy every row has to pass as well.

    Raises:
        ConfigurationError: The command is unknown, a role isn't a non-empty string, neither
            condition is given, a condition isn't a ``Q``/``RawSQLTerm``, or the command takes no
            such condition.
    """

    command: PolicyCommand = PolicyCommand.ALL
    roles: tuple[str, ...] = ()
    using: Q | RawSQLTerm | TenantCondition | None = None
    with_check: Q | RawSQLTerm | TenantCondition | None = None
    permissive: bool = True

    def __post_init__(self) -> None:
        super().__post_init__()
        if self.command not in set(PolicyCommand):
            raise ConfigurationError(
                f"Policy {self.name!r}: command must be one of {', '.join(PolicyCommand)}, got {self.command!r}"
            )
        if isinstance(cast("object", self.roles), str) or not all(
            isinstance(role, str) and role for role in self.roles
        ):
            raise ConfigurationError(
                f"Policy {self.name!r}: roles must be a sequence of role names, got {self.roles!r}"
            )
        object.__setattr__(self, "roles", tuple(self.roles))
        if self.using is None and self.with_check is None:
            raise ConfigurationError(f"Policy {self.name!r}: give a using or a with_check condition")
        if self.using is not None:
            if self.command in POLICY_COMMANDS_WITHOUT_USING:
                raise ConfigurationError(f"Policy {self.name!r}: an {self.command} policy takes only with_check")
            if not isinstance(self.using, TenantCondition):
                ConstraintCondition.raise_if_not_condition(self.using, f"Policy {self.name!r} using")
        if self.with_check is not None:
            if self.command in POLICY_COMMANDS_WITHOUT_WITH_CHECK:
                raise ConfigurationError(f"Policy {self.name!r}: a {self.command} policy takes only using")
            if not isinstance(self.with_check, TenantCondition):
                ConstraintCondition.raise_if_not_condition(self.with_check, f"Policy {self.name!r} with_check")
        if not isinstance(self.permissive, bool):
            raise ConfigurationError(f"Policy {self.name!r}: permissive must be a bool, got {self.permissive!r}")

    def deconstruct(self) -> tuple[str, list[Any], dict[str, Any]]:
        kwargs: dict[str, Any] = {"name": self.name}
        if self.command != PolicyCommand.ALL:
            kwargs["command"] = PolicyCommand(self.command)
        if self.roles:
            kwargs["roles"] = self.roles
        if self.using is not None:
            kwargs["using"] = self.using
        if self.with_check is not None:
            kwargs["with_check"] = self.with_check
        if not self.permissive:
            kwargs["permissive"] = False
        return ClassPath.get(type(self)), [], kwargs
