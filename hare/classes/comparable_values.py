from __future__ import annotations

import dataclasses
import datetime as dt
import functools
import ipaddress
import json
import types
import typing
import uuid
import zoneinfo
from decimal import Decimal
from enum import Enum
from typing import Any

from hare.classes.class_path import ClassPath
from hare.classes.constants import FUNCTION_DECLARATION_PATH_ATTRIBUTE, UNSUPPORTED_COMPARABLE_VALUE
from hare.exceptions import ConfigurationError


class ComparableValues:
    """Values two equal declarations give alike - a declaration rebuilt from a migration file is a new
    object, and hare's expressions compare by identity, so declarations are compared by the call a
    migration file writes them as (``deconstruct()``)."""

    @classmethod
    def get(cls, value: Any) -> object:
        """A typed structural value, independent of mapping and set iteration order.

        Lists and tuples are equivalent declaration sequences. Scalar types and
        mapping keys stay distinct; display strings never establish equality.

        Args:
            value: The value.

        Returns:
            The comparable structure, containing JSON-serializable values.
        """
        if isinstance(value, Enum):
            return ["enum_member", cls.get(type(value)), cls.get(value.value)]
        if value is None:
            return ["none"]
        if isinstance(value, bool):
            return ["bool", value]
        if isinstance(value, int):
            return ["int", value]
        if isinstance(value, float):
            return ["float", value.hex()]
        if isinstance(value, str):
            return ["str", value]
        if isinstance(value, bytes):
            return ["bytes", value.hex()]
        if isinstance(value, dict):
            entries = [[cls.get(key), cls.get(item)] for key, item in value.items()]
            return ["mapping", sorted(entries, key=lambda entry: json.dumps(entry[0], sort_keys=True))]
        if isinstance(value, (list, tuple)):
            return ["sequence", [cls.get(item) for item in value]]
        if isinstance(value, (set, frozenset)):
            return [
                "set",
                sorted((cls.get(item) for item in value), key=lambda item: json.dumps(item, sort_keys=True)),
            ]
        return cls.get_object_value(value)

    @classmethod
    def get_object_value(cls, value: Any) -> object:
        """An object as two equal declarations give it: a class by its path; an object a migration file
        writes as a call (``deconstruct()``) by that call. Standard-library values
        retain their type and constructor data; unsupported objects fail explicitly.

        Args:
            value: The object.

        Returns:
            The comparable value.
        """
        if isinstance(value, type) and issubclass(value, Enum):
            # By what it holds - a migration file declares the enum anew, one class per file.
            bases, name, members = cls.get_enum_content(value)
            return [
                "enum",
                list(bases),
                name,
                [[member_name, cls.get(member_value)] for member_name, member_value in members],
            ]
        if isinstance(value, type):
            return ["type", getattr(value, "migration_import_path", None) or ClassPath.get(value)]
        if import_path := getattr(value, "migration_import_path", None):
            return ["reference", import_path]
        deconstruct = getattr(value, "deconstruct", None)
        if callable(deconstruct):
            path, args, kwargs = deconstruct()
            return ["declaration", path, cls.get(args), cls.get(kwargs)]
        if dataclasses.is_dataclass(value):
            return [
                "dataclass",
                ClassPath.get(type(value)),
                cls.get({field.name: getattr(value, field.name) for field in dataclasses.fields(value) if field.init}),
            ]
        if isinstance(value, Decimal):
            return ["decimal", str(value)]
        if isinstance(value, uuid.UUID):
            return ["uuid", value.hex]
        if isinstance(value, dt.timedelta):
            return ["timedelta", value.days, value.seconds, value.microseconds]
        if isinstance(value, (dt.datetime, dt.time)):
            zone = value.tzinfo
            zone_value = ["zone", zone.key] if isinstance(zone, zoneinfo.ZoneInfo) else cls.get(value.utcoffset())
            return [
                "datetime" if isinstance(value, dt.datetime) else "time",
                value.isoformat(),
                value.fold,
                zone_value,
            ]
        if isinstance(value, dt.date):
            return ["date", value.isoformat()]
        if isinstance(value, zoneinfo.ZoneInfo):
            return ["zone", value.key]
        if isinstance(value, dt.timezone):
            return ["timezone", cls.get(value.utcoffset(None)), value.tzname(None)]
        if isinstance(value, (ipaddress.IPv4Interface, ipaddress.IPv6Interface)):
            return ["ip_interface", value.version, value.with_prefixlen]
        if isinstance(value, (ipaddress.IPv4Address, ipaddress.IPv6Address)):
            return ["ip_address", value.version, value.packed.hex()]
        if isinstance(value, (ipaddress.IPv4Network, ipaddress.IPv6Network)):
            return ["ip_network", value.version, value.with_prefixlen]
        if isinstance(value, functools.partial):
            return ["partial", cls.get(value.func), cls.get(value.args), cls.get(value.keywords)]
        if isinstance(value, types.FunctionType):
            return ["function", getattr(value, FUNCTION_DECLARATION_PATH_ATTRIBUTE, None) or ClassPath.get(value)]
        if isinstance(value, (types.BuiltinFunctionType, types.MethodType)):
            bound_to = getattr(value, "__self__", None)
            if isinstance(bound_to, type):
                return ["class_method", cls.get(bound_to), value.__name__]
            if bound_to is None or isinstance(bound_to, types.ModuleType):
                return ["function", ClassPath.get(value)]
        origin = typing.get_origin(value)
        if origin is not None:
            arguments = cls.get(typing.get_args(value))
            if origin in (typing.Union, types.UnionType):
                return ["union", arguments]
            return ["generic", cls.get(origin), getattr(value, "_name", None), arguments]
        if value is Ellipsis:
            return ["ellipsis"]
        typing_name = getattr(value, "_name", None)
        if typing_name and getattr(typing, typing_name, None) is value:
            return ["typing", typing_name]
        raise ConfigurationError(UNSUPPORTED_COMPARABLE_VALUE.format(type=ClassPath.get(type(value))))

    @staticmethod
    def get_enum_content(enum_type: type[Enum]) -> tuple[tuple[str, ...], str, tuple[tuple[str, Any], ...]]:
        """What an enum holds - its bases, its name and its members in order - equal for two enums
        built the same way, one class or two.

        Args:
            enum_type: The enum.

        Returns:
            ``(bases, name, ((member name, value), ...))``, the bases as dotted paths.
        """
        return (
            tuple(ClassPath.get(base) for base in enum_type.__bases__),
            enum_type.__name__,
            tuple((name, member.value) for name, member in enum_type.__members__.items()),
        )

    @classmethod
    def get_declaration_key(cls, declaration: Any, *, without_name: bool = False) -> str:
        """A hashable text of a declaration's comparable value.

        Args:
            declaration: An object with ``deconstruct()`` - an index, a constraint, a view.
            without_name: Leave its ``name`` out - what tells a renamed declaration.

        Returns:
            The text.
        """
        path, args, kwargs = declaration.deconstruct()
        if without_name:
            kwargs = {name: value for name, value in kwargs.items() if name != "name"}
        return json.dumps([path, cls.get(args), cls.get(kwargs)], sort_keys=True)

    @classmethod
    def are_equal(cls, first: Any, second: Any) -> bool:
        """Whether two declarations are equal by their comparable values.

        Args:
            first: A declaration.
            second: Another one.
        """
        return cls.get(first) == cls.get(second)
