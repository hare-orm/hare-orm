from __future__ import annotations

from typing import Any

from hare.contrib.request_query.constants import META_OPTION_MEMBERS
from hare.exceptions import ConfigurationError
from hare.query.enums import Connector


class MetaOptionChecks:
    """Checks the structure used by request query options, without requiring their classes.

    Collection items are checked by their consumers, so validation does not consume an
    iterable before its declaration is built. Option methods keep responsibility for their
    own arguments and results; checking structure does not call them speculatively.
    """

    @staticmethod
    def check_option(owner: str, name: str, value: Any) -> None:
        """Checks one resolved ``Meta`` option.

        Args:
            owner: The request query class's qualified name.
            name: The option name.
            value: The declared value.

        Raises:
            ConfigurationError: An option lacks a required member, a collection cannot be
                iterated, or the condition connector is not supported.
        """
        owner = f"{owner}.Meta.{name}"
        if name == "join_type":
            if value not in (Connector.AND, Connector.OR):
                raise ConfigurationError(f"{owner} takes AND or OR")
        elif value is not None:
            if name in ("filters", "api_getter"):
                try:
                    iter(value)
                except TypeError as error:
                    raise ConfigurationError(f"{owner} must be iterable") from error
            else:
                methods, attributes = META_OPTION_MEMBERS.get(name, ((), ()))
                MetaOptionChecks.check_members(owner, value, methods, attributes)

    @staticmethod
    def check_members(owner: str, value: Any, methods: tuple[str, ...], attributes: tuple[str, ...]) -> None:
        """Requires callable methods and readable attributes used by an option's consumer.

        Args:
            owner: The declaration's location, included in the error.
            value: The option or collection item.
            methods: The methods the consumer calls.
            attributes: The attributes the consumer reads, including those allowed to be None.

        Raises:
            ConfigurationError: A required method is absent or not callable, or an attribute
                is absent. The error names every missing member.
        """
        missing = [f"{name}()" for name in methods if not callable(getattr(value, name, None))]
        missing.extend(name for name in attributes if not hasattr(value, name))
        if missing:
            raise ConfigurationError(f"{owner} requires {', '.join(missing)}")
