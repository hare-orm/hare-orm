from __future__ import annotations

from dataclasses import dataclass
from typing import Any, cast

from hare.classes.class_path import ClassPath
from hare.ddl.constants import FUNCTION_LANGUAGE_PATTERN
from hare.ddl.enums import FunctionVolatility
from hare.ddl.raw_sql_term import RawSQLTerm
from hare.ddl.schema_objects.named_schema_object import NamedSchemaObject
from hare.exceptions import ConfigurationError


@dataclass(frozen=True)
class DatabaseFunction(NamedSchemaObject):
    """A function stored in the database a model declares in ``Meta.functions`` - in the model's
    schema, called from SQL: a view, a policy condition, a column default, a raw query.

    Args:
        name: The function's name.
        returns: Its result type as the database writes it (``"integer"``, ``"SETOF text"``,
            ``"TABLE (id integer, total numeric)"``).
        body: ``RawSQLTerm`` of its body in its language, without the quoting around it. Not
            portable.
        arguments: Its arguments as the database writes them (``"tenant_id integer"``) - with the
            name, the function is known by their types.
        language: Its language (``"sql"`` or a procedural one); None for the dialect's default.
        volatility: What it promises about its result - ``FunctionVolatility``.
        security_definer: Run with the privileges of its owner instead of the caller's.

    Raises:
        ConfigurationError: An argument has the wrong type, ``body`` isn't a ``RawSQLTerm``,
            ``returns``/``body``/an argument is empty, or ``language`` isn't a plain name.
    """

    returns: str
    body: RawSQLTerm
    arguments: tuple[str, ...] = ()
    language: str | None = None
    volatility: FunctionVolatility = FunctionVolatility.VOLATILE
    security_definer: bool = False

    def __post_init__(self) -> None:
        super().__post_init__()
        if not isinstance(self.returns, str) or not self.returns.strip():
            raise ConfigurationError(f"DatabaseFunction {self.name!r}: returns must be a non-empty string")
        if not isinstance(self.body, RawSQLTerm):
            raise ConfigurationError(
                f"DatabaseFunction {self.name!r}: body takes RawSQLTerm(...) of raw SQL, got {self.body!r}"
            )
        if not isinstance(self.body.sql, str) or not self.body.sql.strip():
            raise ConfigurationError(f"DatabaseFunction {self.name!r}: the body can't be empty")
        if isinstance(cast("object", self.arguments), str) or not all(
            isinstance(argument, str) and argument.strip() for argument in self.arguments
        ):
            raise ConfigurationError(
                f"DatabaseFunction {self.name!r}: arguments must be a sequence of non-empty strings, "
                f"got {self.arguments!r}"
            )
        object.__setattr__(self, "arguments", tuple(self.arguments))
        if self.language is not None and (
            not isinstance(self.language, str) or not FUNCTION_LANGUAGE_PATTERN.fullmatch(self.language)
        ):
            raise ConfigurationError(
                f"DatabaseFunction {self.name!r}: language must be a plain name such as 'sql', got {self.language!r}"
            )
        if self.volatility not in set(FunctionVolatility):
            raise ConfigurationError(
                f"DatabaseFunction {self.name!r}: volatility must be one of {', '.join(FunctionVolatility)}, "
                f"got {self.volatility!r}"
            )
        if not isinstance(self.security_definer, bool):
            raise ConfigurationError(
                f"DatabaseFunction {self.name!r}: security_definer must be a bool, got {self.security_definer!r}"
            )

    def deconstruct(self) -> tuple[str, list[Any], dict[str, Any]]:
        kwargs: dict[str, Any] = {"name": self.name, "returns": self.returns, "body": self.body}
        if self.arguments:
            kwargs["arguments"] = self.arguments
        if self.language is not None:
            kwargs["language"] = self.language
        if self.volatility != FunctionVolatility.VOLATILE:
            kwargs["volatility"] = FunctionVolatility(self.volatility)
        if self.security_definer:
            kwargs["security_definer"] = True
        return ClassPath.get(type(self)), [], kwargs
