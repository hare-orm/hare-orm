from __future__ import annotations

import ast
import datetime as dt
import functools
import importlib
import inspect
import ipaddress
import math
import subprocess  # nosec B404 - runs ruff only
import sys
import textwrap
import types
import typing
import unicodedata
import uuid
import zoneinfo
from collections.abc import Iterable
from decimal import Decimal
from enum import Enum, Flag
from pathlib import Path
from typing import Any

from hare.classes.class_path import ClassPath
from hare.ddl.conditions.enum_members_condition import EnumMembersCondition
from hare.ddl.conditions.exclusive_arc_condition import ExclusiveArcCondition
from hare.ddl.constraints.check_constraint import CheckConstraint
from hare.ddl.constraints.exclusion_constraint import ExclusionConstraint
from hare.ddl.constraints.unique_constraint import UniqueConstraint
from hare.ddl.indexes.index import Index
from hare.ddl.indexes.ordered_index_key import OrderedIndexKey
from hare.ddl.schema_objects.trigger import Trigger
from hare.exceptions import ConfigurationError
from hare.fields.db_defaults.now import Now
from hare.fields.db_defaults.random_hex import RandomHex
from hare.fields.db_defaults.sql_default import SqlDefault
from hare.fields.db_defaults.uuid_v7 import UuidV7
from hare.fields.relations.swappable_model_reference import SwappableModelReference
from hare.migrations.operations import Operation
from hare.migrations.runtime_enums import RuntimeEnums
from hare.migrations.swappable_dependency import SwappableDependency
from hare.migrations.writer.constants import (
    DIALECT_FIELDS_PACKAGE_SEPARATOR,
    MIGRATION_FORMATTER_RUFF_ARGUMENTS,
    MIGRATION_LINE_LENGTH,
    MIGRATION_SLUG_MAX_LENGTH,
    MIGRATION_SLUG_RE,
    MIGRATION_UNDERSCORE_RUN_PATTERN,
)
from hare.migrations.writer.eager_reference_rewriter import EagerReferenceRewriter
from hare.migrations.writer.import_manager import ImportManager
from hare.query.enums import Connector
from hare.query.expressions import Expression, F, Ordering, Q
from hare.sql.constants import NEUTRAL_SQL_CONTEXT


class MigrationWriter:
    """Writes a migration file of operations."""

    @staticmethod
    def slugify_name(value: str) -> str:
        """The name of a migration file after its number - word characters joined by underscores, cut
        to ``MIGRATION_SLUG_MAX_LENGTH``.

        Args:
            value: The name given.

        Returns:
            The name; ``auto`` when nothing of it is left.
        """
        slug = unicodedata.normalize("NFKC", value).strip().lower().replace(" ", "_")
        slug = MIGRATION_SLUG_RE.sub("_", slug)
        slug = MIGRATION_UNDERSCORE_RUN_PATTERN.sub("_", slug).strip("_")
        slug = slug[:MIGRATION_SLUG_MAX_LENGTH].rstrip("_")
        return slug or "auto"

    @staticmethod
    def format_name(number: int, name: str) -> str:
        return f"{number:04d}_{MigrationWriter.slugify_name(name)}"

    @staticmethod
    def module_path(module_name: str) -> Path:
        module = importlib.import_module(module_name)
        if not hasattr(module, "__path__"):
            raise ConfigurationError(f"Migration module {module_name} is not a package")
        return Path(next(iter(module.__path__)))

    @staticmethod
    def _get_import(value: Any) -> tuple[str, str, bool]:
        # A class can declare its own "stable" import path (`migration_import_path`) - without this,
        # moving a custom class to another module silently breaks ALREADY-generated migrations, since
        # `__module__` records its physical location at generation time.
        override_path = getattr(value, "migration_import_path", None)
        if override_path:
            module_name, _, name = override_path.rpartition(".")
            return module_name, name, False
        module_name = ClassPath.get_module_name(value)
        if not module_name:
            raise ConfigurationError(f"Cannot resolve import for {value!r}")
        module = importlib.import_module(module_name)
        # Only a name an import statement takes - pydantic keeps the class it makes for
        # GenericModel[int] in its module under "GenericModel[int]".
        for name, obj in module.__dict__.items():
            if obj is value and name.isidentifier():
                return module_name, name, False
        qualname = getattr(value, "__qualname__", None)
        if qualname and "." in qualname:
            if "<locals>" in qualname:
                raise ConfigurationError(f"Cannot resolve import for {value!r}")
            return module_name, f"{module_name}.{qualname}", True
        name = getattr(value, "__name__", None)
        if name and name.isidentifier() and getattr(module, name, None) is value:
            return module_name, name, False
        raise ConfigurationError(f"Cannot resolve import for {value!r}")

    @staticmethod
    def render_date_or_time(value: dt.datetime | dt.date | dt.time, imports: ImportManager) -> str:
        """Renders a date/datetime/time as the plain ``datetime`` class it is (a subclass such as
        freezegun's ``FakeDatetime`` included), keeping its ``fold`` and its named zone.

        Args:
            value: The value.
            imports: Collects the imports the rendered code needs.

        Returns:
            Python code rebuilding an equal value.
        """
        module_name = imports.add_module("datetime")
        if isinstance(value, dt.datetime):
            base_class_name = "datetime"
        elif isinstance(value, dt.date):
            return f"{module_name}.date.fromisoformat({dt.date.isoformat(value)!r})"
        else:
            base_class_name = "time"
        tzinfo = value.tzinfo
        zone_key = getattr(tzinfo, "key", None)
        keeps_named_zone = isinstance(tzinfo, zoneinfo.ZoneInfo) and isinstance(zone_key, str)
        iso_text = (value.replace(tzinfo=None) if keeps_named_zone else value).isoformat()
        rendered = f"{module_name}.{base_class_name}.fromisoformat({iso_text!r})"
        replacements = []
        if keeps_named_zone:
            zone_class_name = MigrationWriter.import_object(type(tzinfo), imports)
            replacements.append(f"tzinfo={zone_class_name}({zone_key!r})")
        if value.fold:
            replacements.append(f"fold={value.fold!r}")
        if replacements:
            rendered += f".replace({', '.join(replacements)})"
        return rendered

    @staticmethod
    def import_object(value: Any, imports: ImportManager) -> str:
        """Adds the import of a class or a function to the migration.

        Args:
            value: The class or the function.
            imports: Collects the imports the rendered code needs.

        Returns:
            The name the migration refers to it by.
        """
        module_name, name, use_module = MigrationWriter._get_import(value)
        if use_module:
            module_reference = imports.add_module(module_name)
            return f"{module_reference}{name[len(module_name) :]}"
        return imports.add_from(module_name, name)

    @staticmethod
    def render_value(value: Any, imports: ImportManager) -> str:
        """A value as the Python code rebuilding it.

        Args:
            value: The value.
            imports: Collects the imports the code needs.

        Returns:
            The code.

        Raises:
            ConfigurationError: The value can't be written into a migration.
        """
        for renderer in (
            # StrEnum instances are both str and Enum, so the enum goes before the literals
            MigrationWriter.render_enum_member,
            MigrationWriter.render_literal,
            MigrationWriter.render_database_default,
            MigrationWriter.render_query_expression,
            MigrationWriter.render_schema_object,
            MigrationWriter.render_collection,
            MigrationWriter.render_standard_library_value,
            MigrationWriter.render_partial,
            # a parameterized type passes for its class (isinstance(dict[str, int], type)), so it goes first
            MigrationWriter.render_type_expression,
            # classes are callable too, so the class goes before the function
            MigrationWriter.render_class,
            MigrationWriter.render_function,
            MigrationWriter.render_self_describing_value,
        ):
            rendered = renderer(value, imports)
            if rendered is not None:
                return rendered
        return MigrationWriter.render_literal_text(value)

    @staticmethod
    def render_literal_text(value: Any) -> str:
        """A value no renderer knows as its text - only when the text reads back as the value itself.

        Args:
            value: The value.

        Returns:
            Its ``repr()``.

        Raises:
            ConfigurationError: The text is no Python literal of the value - a migration file would
                fail to load, or rebuild another value.
        """
        text = repr(value)
        try:
            rebuilt = ast.literal_eval(text)
        except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError):
            rebuilt = None
        else:
            if type(rebuilt) is type(value) and rebuilt == value:
                return text
        raise ConfigurationError(
            f"{text[:200]} ({type(value).__qualname__}) can't be written into a migration - use a value of a "
            "type a migration writes (a literal, a collection of them, a Decimal, a UUID, a date or time, an "
            "IP address, an enum member) or give it a deconstruct()"
        )

    @staticmethod
    def render_enum_member(value: Any, imports: ImportManager) -> str | None:
        """An enum member as code.

        Args:
            value: The value.
            imports: Collects the imports the code needs.

        Returns:
            The code, None when the value isn't an enum member.
        """
        if not isinstance(value, Enum):
            return None
        enum_class = value.__class__
        if MigrationWriter.is_imported_enum(enum_class, imports):
            name = MigrationWriter.import_object(enum_class, imports)
        else:
            name = MigrationWriter.declare_enum(enum_class, imports)
        if isinstance(value, Flag) and value.name not in enum_class.__members__:
            # A combination of flags (or an empty one) has no member of its own to name.
            return f"{name}({value.value!r})"
        return f"{name}.{value.name}"

    @staticmethod
    def render_literal(value: Any, imports: ImportManager) -> str | None:
        """A value Python writes as a literal as code.

        Args:
            value: The value.
            imports: Collects the imports the code needs.

        Returns:
            The code, None when the value isn't None, a boolean, a number, a string or bytes.
        """
        if isinstance(value, float) and not math.isfinite(value):
            return f"float({str(value)!r})"
        if value is None or isinstance(value, (bool, int, float, str, bytes)):
            return repr(value)
        return None

    @staticmethod
    def render_database_default(value: Any, imports: ImportManager) -> str | None:
        """A database-side default as code.

        Args:
            value: The value.
            imports: Collects the imports the code needs.

        Returns:
            The code, None when the value isn't a database-side default.
        """
        if not isinstance(value, SqlDefault):
            return None
        for default_class in (Now, RandomHex, UuidV7):
            if isinstance(value, default_class):
                return f"{imports.add_from('hare.fields.db_defaults', default_class.__name__)}()"
        path, args, kwargs = value.deconstruct()
        module_name, _, class_name = path.rpartition(".")
        if (
            type(value) is not SqlDefault
            and MigrationWriter.is_importable_module_name(module_name)
            and class_name.isidentifier()
        ):
            # A subclass keeps its class - a dialect's renderer of it, its own SQL.
            return MigrationWriter.render_call(path, args, kwargs, imports)
        return f"{imports.add_from('hare.fields.db_defaults', 'SqlDefault')}({value.sql!r})"

    @staticmethod
    def render_query_expression(value: Any, imports: ImportManager) -> str | None:
        """A condition, a column reference, an ordering or another expression as code.

        Args:
            value: The value.
            imports: Collects the imports the code needs.

        Returns:
            The code, None when the value isn't one of them.
        """
        if isinstance(value, Q):
            return MigrationWriter.render_condition(value, imports)
        if type(value) is F:
            return f"{imports.add_from('hare.query.expressions', 'F')}({value.name!r})"
        if isinstance(value, Ordering):
            direction = "asc" if value.order.is_ascending else "desc"
            nulls = {True: "nulls_first=True", False: "nulls_last=True", None: ""}[value.order.nulls_first]
            if isinstance(value.target, str):
                return f"{imports.add_from('hare.query.expressions', 'F')}({value.target!r}).{direction}({nulls})"
            return f"{MigrationWriter.render_query_expression(value.target, imports)}.{direction}({nulls})"
        if isinstance(value, OrderedIndexKey):
            key_name = imports.add_from("hare.ddl.indexes", "OrderedIndexKey")
            term_name = imports.add_from("hare.ddl", "RawSQLTerm")
            order_name = imports.add_from("hare.sql", "Order")
            return (
                f"{key_name}({term_name}({value.term.get_sql(NEUTRAL_SQL_CONTEXT)!r}), "
                f"{order_name}.{value.order.name})"
            )
        if isinstance(value, Expression):
            # The expression itself, not its SQL: the database a migration runs on renders it.
            expression_path, expression_args, expression_kwargs = value.deconstruct()
            return MigrationWriter.render_call(expression_path, expression_args, expression_kwargs, imports)
        return None

    @staticmethod
    def render_schema_object(value: Any, imports: ImportManager) -> str | None:
        """An index, a constraint, a trigger or a SQL term as code.

        Args:
            value: The value.
            imports: Collects the imports the code needs.

        Returns:
            The code, None when the value is none of them.
        """
        if isinstance(
            value,
            (
                Index,
                UniqueConstraint,
                CheckConstraint,
                ExclusionConstraint,
                ExclusiveArcCondition,
                EnumMembersCondition,
                Trigger,
            ),
        ):
            # A schema object renders its own SQL for a model - written as the call building it.
            object_path, object_args, object_kwargs = value.deconstruct()
            return MigrationWriter.render_call(object_path, object_args, object_kwargs, imports)
        if hasattr(value, "get_sql") and callable(value.get_sql):
            sql = value.get_sql(NEUTRAL_SQL_CONTEXT)
            return f"{imports.add_from('hare.ddl', 'RawSQLTerm')}({sql!r})"
        return None

    @staticmethod
    def render_collection(value: Any, imports: ImportManager) -> str | None:
        """A list, a tuple, a dict, a set or a frozenset as code.

        Args:
            value: The value.
            imports: Collects the imports the code needs.

        Returns:
            The code, None when the value is none of them.
        """
        if isinstance(value, list):
            return "[" + ", ".join(MigrationWriter.render_value(item, imports) for item in value) + "]"
        if isinstance(value, tuple):
            if len(value) == 1:
                return f"({MigrationWriter.render_value(value[0], imports)},)"
            return "(" + ", ".join(MigrationWriter.render_value(item, imports) for item in value) + ")"
        if isinstance(value, dict):
            items = [
                f"{MigrationWriter.render_value(key, imports)}: {MigrationWriter.render_value(item_value, imports)}"
                for key, item_value in value.items()
            ]
            return "{" + ", ".join(items) + "}"
        if isinstance(value, (set, frozenset)):
            # Sorted by their code - a set has no order, a file written twice is the same.
            items_code = sorted(MigrationWriter.render_value(item, imports) for item in value)
            if isinstance(value, frozenset):
                return "frozenset({" + ", ".join(items_code) + "})" if items_code else "frozenset()"
            return "{" + ", ".join(items_code) + "}" if items_code else "set()"
        return None

    @staticmethod
    def render_standard_library_value(value: Any, imports: ImportManager) -> str | None:
        """A decimal, a UUID, a date, a time, a time difference or an IP address or network as code.

        Args:
            value: The value.
            imports: Collects the imports the code needs.

        Returns:
            The code, None when the value is none of them.
        """
        if isinstance(value, Decimal):
            return f"{imports.add_from('decimal', 'Decimal')}({str(value)!r})"
        if isinstance(value, uuid.UUID):
            return f"{imports.add_module('uuid')}.UUID({str(value)!r})"
        if isinstance(value, dt.timedelta):
            module_name = imports.add_module("datetime")
            # Exact integer components - total_seconds() loses microseconds for a large timedelta.
            return (
                f"{module_name}.timedelta(days={value.days!r}, seconds={value.seconds!r}, "
                f"microseconds={value.microseconds!r})"
            )
        if isinstance(value, (dt.datetime, dt.date, dt.time)):
            return MigrationWriter.render_date_or_time(value, imports)
        # An interface is an address too - it goes first.
        if isinstance(value, (ipaddress.IPv4Interface, ipaddress.IPv6Interface)):
            return f"{imports.add_module('ipaddress')}.ip_interface({str(value)!r})"
        if isinstance(value, (ipaddress.IPv4Address, ipaddress.IPv6Address)):
            return f"{imports.add_module('ipaddress')}.ip_address({str(value)!r})"
        if isinstance(value, (ipaddress.IPv4Network, ipaddress.IPv6Network)):
            return f"{imports.add_module('ipaddress')}.ip_network({str(value)!r})"
        return None

    @staticmethod
    def render_partial(value: Any, imports: ImportManager) -> str | None:
        """A ``functools.partial`` as code.

        Args:
            value: The value.
            imports: Collects the imports the code needs.

        Returns:
            The code, None when the value isn't a partial.

        Raises:
            ConfigurationError: The partial wraps a lambda or a local function.
        """
        if not isinstance(value, functools.partial):
            return None
        function = value.func
        if getattr(function, "__name__", "") == "<lambda>":
            raise ConfigurationError("Cannot serialize lambda inside functools.partial; use a module-level function.")
        if hasattr(function, "__qualname__") and "<locals>" in function.__qualname__:
            raise ConfigurationError(f"Cannot serialize partial with local function: {function!r}")
        arguments = [MigrationWriter.render_value(argument, imports) for argument in value.args]
        arguments += [
            f"{key}={MigrationWriter.render_value(item_value, imports)}" for key, item_value in value.keywords.items()
        ]
        function_reference = MigrationWriter.import_object(function, imports)
        module_name = imports.add_module("functools")
        return f"{module_name}.partial({', '.join([function_reference, *arguments])})"

    @staticmethod
    def render_type_expression(value: Any, imports: ImportManager) -> str | None:
        """A parameterized type as code - ``dict[str, int]``, ``list[Model]``, ``int | None``,
        ``typing.Literal["a", "b"]``, ``typing.List[int]`` - its origin and each argument written by
        their own renderers.

        Args:
            value: The value.
            imports: Collects the imports the code needs.

        Returns:
            The code, None when the value isn't a parameterized type.

        Raises:
            ConfigurationError: Its origin can't be written into a migration - a type alias statement's.
        """
        origin = typing.get_origin(value)
        if origin is None:
            return None
        arguments = typing.get_args(value)
        if isinstance(value, types.UnionType) or origin is typing.Union:
            return " | ".join(MigrationWriter.render_type_argument(argument, imports) for argument in arguments)
        typing_name = MigrationWriter.get_typing_name(value, origin)
        if typing_name is not None:
            # A typing alias (List, Dict) or a special form (Literal, Annotated) - not equal to the
            # builtin's own parameterized type, so written by its typing name.
            origin_text = f"{imports.add_module('typing')}.{typing_name}"
        else:
            origin_text = MigrationWriter.render_value(origin, imports)
        if not arguments:
            return f"{origin_text}[()]"
        argument_texts = [MigrationWriter.render_type_argument(argument, imports) for argument in arguments]
        return f"{origin_text}[{', '.join(argument_texts)}]"

    @staticmethod
    def get_typing_name(value: Any, origin: Any) -> str | None:
        """The name the typing module gives a parameterized type's origin - ``List`` of
        ``typing.List[int]``, ``Literal`` of ``typing.Literal["a"]``.

        Args:
            value: The parameterized type.
            origin: Its origin.

        Returns:
            The name, None for a type parameterized by itself (``list[int]``, a generic class).
        """
        if isinstance(value, types.GenericAlias):
            return None
        name = getattr(value, "_name", None)
        if name is None and not isinstance(origin, type):
            name = getattr(origin, "_name", None)
        if name is None or getattr(typing, name, None) is None:
            return None
        return str(name)

    @staticmethod
    def render_type_argument(value: Any, imports: ImportManager) -> str:
        """An argument of a parameterized type as code - ``None`` for its ``NoneType``, ``...`` for the
        ellipsis of ``tuple[int, ...]``.

        Args:
            value: The argument.
            imports: Collects the imports the code needs.

        Returns:
            The code.
        """
        if value is type(None):
            return "None"
        if value is Ellipsis:
            return "..."
        return MigrationWriter.render_value(value, imports)

    @staticmethod
    def render_class(value: Any, imports: ImportManager) -> str | None:
        """A class as code.

        Args:
            value: The value.
            imports: Collects the imports the code needs.

        Returns:
            The code, None when the value isn't a class.
        """
        if not isinstance(value, type):
            return None
        if value.__module__ == "builtins":
            return value.__name__
        if issubclass(value, Enum) and not MigrationWriter.is_imported_enum(value, imports):
            return MigrationWriter.declare_enum(value, imports)
        return MigrationWriter.import_object(value, imports)

    @staticmethod
    def is_imported_enum(enum_type: type[Enum], imports: ImportManager) -> bool:
        """Whether a migration imports an enum rather than declaring it itself.

        Args:
            enum_type: The enum.
            imports: Collects the imports and declarations the migration needs.

        Returns:
            False for an enum it can't import, and for one whose members it keeps as they are now
            (``ImportManager.snapshot_enum_types``).
        """
        return enum_type not in imports.snapshot_enum_types and RuntimeEnums.is_importable(enum_type)

    @staticmethod
    def render_function(value: Any, imports: ImportManager) -> str | None:
        """A function or another callable as code.

        Args:
            value: The value.
            imports: Collects the imports the code needs.

        Returns:
            The code, None when the value can't be called.

        Raises:
            ConfigurationError: The value is a lambda or a local function.
        """
        if inspect.isfunction(value) and not MigrationWriter.is_importable_module_name(value.__module__):
            return MigrationWriter.declare_function(value, imports)
        if not callable(value):
            return None
        if getattr(value, "__name__", "") == "<lambda>":
            raise ConfigurationError("Cannot serialize lambda; use a module-level function instead.")
        owner_class = getattr(value, "__self__", None)
        if getattr(value, "__module__", None) is None and isinstance(owner_class, type):
            # A builtin classmethod (datetime.date.today, datetime.datetime.now, ...) carries
            # no __module__ of its own - it's referenced through the class it's bound to.
            module_name = imports.add_module(owner_class.__module__)
            return f"{module_name}.{owner_class.__qualname__}.{value.__name__}"
        if hasattr(value, "__qualname__") and "<locals>" in value.__qualname__:
            raise ConfigurationError(f"Cannot serialize local function: {value!r}")
        return MigrationWriter.import_object(value, imports)

    @staticmethod
    def render_self_describing_value(value: Any, imports: ImportManager) -> str | None:
        """An object that says how it is written as code.

        Args:
            value: The value.
            imports: Collects the imports the code needs.

        Returns:
            The code, None when the object says nothing about it.
        """
        if isinstance(value, SwappableModelReference):
            return f"{imports.add_from('hare.models', 'swappable')}({value.setting!r})"
        # `migration_import_path` imports an existing instance (a Crypto - the constructor would put the
        # key into the file).
        import_path = getattr(value, "migration_import_path", None)
        if import_path:
            module_name, _, name = import_path.rpartition(".")
            return imports.add_from(module_name, name)
        # `deconstruct()` - for values that can be safely and fully rebuilt by calling the constructor
        # with literal arguments.
        if hasattr(value, "deconstruct") and callable(value.deconstruct):
            if getattr(value, "snapshots_enum_in_migrations", False):
                # The enum's members are part of the schema - the migration keeps them as they are.
                imports.snapshot_enum_types.add(value.enum_type)
            path, args, kwargs = value.deconstruct()
            return MigrationWriter.render_call(path, args, kwargs, imports)
        return None

    @staticmethod
    def declare_enum(enum_type: type[Enum], imports: ImportManager) -> str:
        """Declares an enum the migration can't import (``RuntimeEnums.is_importable()``) at the top
        of the migration, in the functional form on the same base -
        ``Status = StrEnum("Status", {"NEW": "new", "DONE": "done"})`` - once per such enum.

        Args:
            enum_type: The enum.
            imports: Collects the imports and declarations the migration needs.

        Returns:
            The name the migration declares the enum under.

        Raises:
            ConfigurationError: The enum's bases can't be written in the functional form - see
                ``RuntimeEnums.get_bases()``.
        """
        content = RuntimeEnums.get_content(enum_type)
        name = imports.enum_names.get(content)
        if name is not None:
            return name
        enum_base, data_type = RuntimeEnums.get_bases(enum_type)
        base_source = MigrationWriter.render_value(enum_base, imports)
        members_source = ", ".join(
            f"{member_name!r}: {MigrationWriter.render_value(member.value, imports)}"
            for member_name, member in enum_type.__members__.items()
        )
        type_source = "" if data_type is None else f", type={MigrationWriter.render_value(data_type, imports)}"
        name = imports.get_free_name(enum_type.__name__)
        imports.enum_names[content] = name
        imports.enum_declarations.append(
            f"{name} = {base_source}({enum_type.__name__!r}, {{{members_source}}}{type_source})"
        )
        return name

    @staticmethod
    def is_importable_module_name(module_name: str) -> bool:
        """Whether a module can be imported by its name in Python code - a migration's module,
        named after its number (``0002_add_title``), can't.

        Args:
            module_name: The dotted module name.

        Returns:
            Whether every part of it is an identifier.
        """
        return all(part.isidentifier() for part in module_name.split("."))

    @staticmethod
    def declare_function(function: Any, imports: ImportManager) -> str:
        """Declares a function of a module the migration can't import - another migration's - at
        the top of the migration: its source, with the imports its body uses moved into the body
        and those its declaration needs kept at module level, once per function.

        Args:
            function: The function.
            imports: Collects the imports and declarations the migration needs.

        Returns:
            The name the migration declares the function under.

        Raises:
            ConfigurationError: The function's source can't be read, it's nested, or it uses other
                names of its module than imports - they wouldn't exist in the new migration.
        """
        function_key = (function.__module__, function.__qualname__)
        name = imports.function_names.get(function_key)
        if name is not None:
            return name
        if "." in function.__qualname__:
            raise ConfigurationError(
                f"{function.__module__}.{function.__qualname__} is nested in another object - a migration "
                "can only copy a module-level function"
            )
        module = sys.modules.get(function.__module__)
        try:
            function_source = textwrap.dedent(inspect.getsource(function))
            module_source = inspect.getsource(module) if module is not None else ""
        except (OSError, TypeError) as error:
            raise ConfigurationError(
                f"The source of {function.__module__}.{function.__qualname__} can't be read to copy it into the "
                "migration"
            ) from error
        function_node = ast.parse(function_source).body[0]
        if not isinstance(function_node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            raise ConfigurationError(f"{function.__module__}.{function.__qualname__} is not a plain function")
        used_names = {node.id for node in ast.walk(function_node) if isinstance(node, ast.Name)}
        body_names = {
            node.id for statement in function_node.body for node in ast.walk(statement) if isinstance(node, ast.Name)
        }
        eager_nodes: list[ast.expr] = [*function_node.decorator_list, *function_node.args.defaults]
        eager_nodes.extend(default for default in function_node.args.kw_defaults if default is not None)
        arguments = (
            *function_node.args.posonlyargs,
            *function_node.args.args,
            *function_node.args.kwonlyargs,
            function_node.args.vararg,
            function_node.args.kwarg,
        )
        eager_nodes.extend(argument.annotation for argument in arguments if argument and argument.annotation)
        if function_node.returns is not None:
            eager_nodes.append(function_node.returns)
        eager_names = {
            node.id for expression in eager_nodes for node in ast.walk(expression) if isinstance(node, ast.Name)
        }
        module_import_nodes: list[ast.stmt] = []
        eager_import_nodes: list[ast.Import | ast.ImportFrom] = []
        module_defined_names: set[str] = set()
        for statement in ast.parse(module_source).body:
            if isinstance(statement, (ast.Import, ast.ImportFrom)):
                body_aliases = [
                    alias for alias in statement.names if (alias.asname or alias.name.split(".", 1)[0]) in body_names
                ]
                eager_aliases = [
                    alias for alias in statement.names if (alias.asname or alias.name.split(".", 1)[0]) in eager_names
                ]
                if body_aliases:
                    module_import_nodes.append(
                        ast.Import(names=body_aliases)
                        if isinstance(statement, ast.Import)
                        else ast.ImportFrom(module=statement.module, names=body_aliases, level=statement.level)
                    )
                if eager_aliases:
                    eager_import_nodes.append(
                        ast.Import(names=eager_aliases)
                        if isinstance(statement, ast.Import)
                        else ast.ImportFrom(module=statement.module, names=eager_aliases, level=statement.level)
                    )
            elif isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                if statement.name != function.__name__:
                    module_defined_names.add(statement.name)
            elif isinstance(statement, (ast.Assign, ast.AnnAssign)):
                targets = statement.targets if isinstance(statement, ast.Assign) else [statement.target]
                module_defined_names.update(
                    node.id for target in targets for node in ast.walk(target) if isinstance(node, ast.Name)
                )
        if any(
            isinstance(node, ast.ImportFrom) and node.level for node in [*module_import_nodes, *eager_import_nodes]
        ):
            raise ConfigurationError(
                f"{function.__module__}.{function.__qualname__} uses a relative import - write it as an absolute "
                "one to copy the function into the migration"
            )
        missing_names = sorted(used_names & module_defined_names)
        if missing_names:
            raise ConfigurationError(
                f"{function.__module__}.{function.__qualname__} uses {', '.join(missing_names)} of its module, which "
                "a copy of the function in another migration wouldn't have - move them into the function or into "
                "an importable module"
            )
        replacements: dict[str, str] = {}
        for statement in eager_import_nodes:
            for alias in statement.names:
                if isinstance(statement, ast.ImportFrom):
                    local_name = alias.asname or alias.name
                    replacements[local_name] = imports.add_from(statement.module or "", alias.name)
                else:
                    replacements[alias.asname or alias.name] = imports.add_module(alias.name)

        rewriter = EagerReferenceRewriter(replacements)
        function_node.decorator_list = [rewriter.visit(expression) for expression in function_node.decorator_list]
        function_node.args = rewriter.visit(function_node.args)
        if function_node.returns is not None:
            function_node.returns = rewriter.visit(function_node.returns)
        name = imports.get_free_name(function.__name__)
        function_node.name = name
        docstring_count = (
            1
            if function_node.body
            and isinstance(function_node.body[0], ast.Expr)
            and isinstance(function_node.body[0].value, ast.Constant)
            and isinstance(function_node.body[0].value.value, str)
            else 0
        )
        function_node.body[docstring_count:docstring_count] = module_import_nodes
        imports.function_names[function_key] = name
        imports.function_declarations.append(ast.unparse(function_node))
        return name

    @staticmethod
    def render_condition(condition: Q, imports: ImportManager) -> str:
        """A ``Q`` condition as the code rebuilding it.

        Args:
            condition: The condition.
            imports: Collects the imports the code needs.

        Returns:
            The code, e.g. ``~Q(qty__lt=0) | Q(price__isnull=True)`` as ``Q(~Q(qty__lt=0), ...)``.

        Raises:
            ConfigurationError: The condition holds an ``Exists(...)``.
        """
        if condition.expression is not None:
            raise ConfigurationError(f"{condition!r}: a Q(Exists(...)) condition can't be written into a migration")
        q_name = imports.add_from("hare.query.expressions", "Q")
        arguments = [MigrationWriter.render_condition(child, imports) for child in condition.children]
        for key, value in condition.filters.items():
            # An expression (F("low") + 1) is written as itself, as an index key is.
            arguments.append(f"{key}={MigrationWriter.render_value(value, imports)}")
        if condition.connector == Connector.OR:
            connector_name = imports.add_from("hare.query.enums", "Connector")
            return (
                f"{condition.get_negation_prefix()}{q_name}.with_connector"
                f"({', '.join([f'{connector_name}.OR', *arguments])})"
            )
        return f"{condition.get_negation_prefix()}{q_name}({', '.join(arguments)})"

    @staticmethod
    def get_public_field_name(path: str) -> str:
        """The ``hare.fields`` attribute a ``hare.fields.*`` path is rendered as - a
        ``*FieldInstance`` class maps to its public ``*Field`` name.

        Args:
            path: Dotted path starting with ``hare.fields.``.

        Returns:
            The attribute name on the ``hare.fields`` package.
        """
        class_name = path.rsplit(".", 1)[1]
        if class_name.endswith("FieldInstance"):
            return class_name.replace("FieldInstance", "Field")
        return class_name

    @staticmethod
    def get_public_field_module(path: str) -> str | None:
        """The ``fields`` package of a dialect exporting the public ``*Field`` of a ``*FieldInstance``
        class of it (``<Name>FieldInstance`` -> ``hare.dialects.<dialect>.fields.<Name>Field``).

        Args:
            path: Dotted path of a field class.

        Returns:
            The package's name; None for another class, or one its package exports no public name of.
        """
        package_name, separator, _rest = path.partition(DIALECT_FIELDS_PACKAGE_SEPARATOR)
        if not separator or not path.rsplit(".", 1)[1].endswith("FieldInstance"):
            return None
        package_name += DIALECT_FIELDS_PACKAGE_SEPARATOR.rstrip(".")
        try:
            package = importlib.import_module(package_name)
        except ImportError:
            return None
        return package_name if hasattr(package, MigrationWriter.get_public_field_name(path)) else None

    @staticmethod
    def is_fields_package_path(path: str) -> bool:
        """Whether a ``hare.fields.*`` path is written as an attribute of the ``hare.fields`` package - the
        package exports the class itself, or, for a ``*FieldInstance`` class, the factory of the same public
        name (``ForeignKeyField``). A class the package doesn't export is imported from its own module.

        Args:
            path: Dotted path starting with ``hare.fields.``.

        Returns:
            True to write it as ``fields.<name>``.
        """
        module_name, class_name = path.rsplit(".", 1)
        public_value = getattr(
            importlib.import_module("hare.fields"), MigrationWriter.get_public_field_name(path), None
        )
        if class_name.endswith("FieldInstance"):
            return callable(public_value)
        return public_value is not None and public_value is getattr(
            importlib.import_module(module_name), class_name, None
        )

    @staticmethod
    def _split_import_path(path: str) -> tuple[str, str, bool]:
        """How a migration imports the object of a dotted path - a nested class (``module.Outer.Inner``) by
        its module and the attributes inside it, anything else from its module.

        Args:
            path: The dotted path.

        Returns:
            The module, the name - dotted inside the module for a nested class - and whether the module is
            imported whole.
        """
        module_name, name = path.rsplit(".", 1)
        try:
            importlib.import_module(module_name)
        except ModuleNotFoundError as error:
            if error.name != module_name:
                raise
            parts = path.split(".")
            for end in range(len(parts) - 1, 0, -1):
                candidate = ".".join(parts[:end])
                try:
                    value: Any = importlib.import_module(candidate)
                except ModuleNotFoundError as candidate_error:
                    if candidate_error.name != candidate:
                        raise
                    continue
                attribute_names = parts[end:]
                for attribute_name in attribute_names:
                    value = getattr(value, attribute_name, None)
                    if value is None:
                        # No such class in the nearest module - written as the path names it.
                        return module_name, name, False
                return candidate, ".".join(attribute_names), True
        return module_name, name, False

    @staticmethod
    def get_callable(path: str) -> Any:
        """The object a ``render_call()`` expression for ``path`` calls once executed.

        Args:
            path: Dotted import path, as passed to ``render_call()``.

        Returns:
            The class or function the rendered call invokes.
        """
        if path.startswith("hare.fields.") and MigrationWriter.is_fields_package_path(path):
            return getattr(importlib.import_module("hare.fields"), MigrationWriter.get_public_field_name(path))
        module_name, name, use_module = MigrationWriter._split_import_path(path)
        value = importlib.import_module(module_name)
        if use_module:
            for part in name.split("."):
                value = getattr(value, part)
            return value
        return getattr(value, name)

    @staticmethod
    def render_call(path: str, args: list[Any], kwargs: dict[str, Any], imports: ImportManager) -> str:
        """The source of a constructor call a migration writes - a field by its public name in ``fields``, a
        relation's model first.

        Args:
            path: The import path of the class.
            args: The positional arguments.
            kwargs: The keyword arguments.
            imports: The imports of the file, added to.

        Returns:
            The source.
        """
        if path.startswith("hare.fields.") and MigrationWriter.is_fields_package_path(path):
            class_name = MigrationWriter.get_public_field_name(path)
            # For relational fields, move model_name to first positional arg
            if (
                path.rsplit(".", 1)[1].endswith("FieldInstance")
                and path.startswith("hare.fields.relations.fields.")
                and "model_name" in kwargs
            ):
                args = [kwargs.pop("model_name")] + list(args)
            callee = f"{imports.add_fields_alias()}.{class_name}"
        elif path.startswith("hare.ddl.indexes."):
            class_name = path.rsplit(".", 1)[1]
            callee = imports.add_index_class(class_name)
        elif path == "hare.ddl.constraints.UniqueConstraint":
            callee = imports.add_constraint_class("UniqueConstraint")
        elif path == "hare.ddl.constraints.CheckConstraint":
            callee = imports.add_constraint_class("CheckConstraint")
        elif path == "hare.ddl.constraints.ExclusionConstraint":
            callee = imports.add_constraint_class("ExclusionConstraint")
        else:
            if (public_field_module := MigrationWriter.get_public_field_module(path)) is not None:
                module, name, use_module = public_field_module, MigrationWriter.get_public_field_name(path), False
            else:
                module, name, use_module = MigrationWriter._split_import_path(path)
            callee = f"{imports.add_module(module)}.{name}" if use_module else imports.add_from(module, name)

        rendered_args = [MigrationWriter.render_value(arg, imports) for arg in args]
        rendered_kwargs = [
            f"{key}={MigrationWriter.render_value(argument_value, imports)}" for key, argument_value in kwargs.items()
        ]
        return f"{callee}({', '.join(rendered_args + rendered_kwargs)})"

    def __init__(
        self,
        name: str,
        app_label: str,
        operations: Iterable[Operation],
        *,
        dependencies: list[tuple[str, str]] | None = None,
        run_before: list[tuple[str, str]] | None = None,
        replaces: list[tuple[str, str]] | None = None,
        initial: bool | None = None,
        migrations_module: str | None = None,
        atomic: bool = True,
    ) -> None:
        self.name = name
        self.app_label = app_label
        self.operations = list(operations)
        self.dependencies = dependencies or []
        self.run_before = run_before or []
        self.replaces = replaces or []
        self.initial = initial
        self.migrations_module = migrations_module
        self.atomic = atomic

    def path(self) -> Path:
        if not self.migrations_module:
            raise ConfigurationError("migrations_module is required to resolve the output path")
        return self.module_path(self.migrations_module) / f"{self.name}.py"

    def write(self) -> Path:
        path = self.path()
        # LF on every platform.
        path.write_text(self.as_string(), encoding="utf-8", newline="\n")
        # A process importing the file right away would otherwise hit a stale directory listing.
        importlib.invalidate_caches()
        return path

    @staticmethod
    def format_files(paths: Iterable[Path]) -> None:
        """Sorts the imports of and formats written migration files with ruff, the way Django runs
        black on them - skipped when ruff isn't installed. A file ruff can't format is left as
        written.

        Args:
            paths: The written migration files.
        """
        file_names = [str(path) for path in paths]
        if not file_names:
            return
        ruff_executable = MigrationWriter.get_ruff_executable()
        if ruff_executable is None:
            return
        for ruff_arguments in MIGRATION_FORMATTER_RUFF_ARGUMENTS:
            subprocess.run(  # nosec B603 - the installed ruff executable and fixed ruff arguments
                [ruff_executable, *ruff_arguments, *file_names],
                capture_output=True,
                check=False,
            )

    @staticmethod
    def get_ruff_executable() -> str | None:
        """The executable of the installed ruff package - run directly, not through a second Python
        interpreter (``python -m ruff``), which would take several times as long to start.

        Returns:
            Its path, None when ruff isn't installed.
        """
        try:
            from ruff.__main__ import find_ruff_bin
        except ImportError:
            return None
        try:
            return find_ruff_bin()
        except FileNotFoundError:
            return None

    def as_string(self) -> str:
        """The source of the migration file.

        Returns:
            The source.
        """
        imports = ImportManager()
        operations = []
        for operation in self.operations:
            operations.extend(self._format_operation(operation, imports, indent=" " * 8))

        lines: list[str] = [
            "from hare import migrations",
            "from hare.migrations import operations as ops",
        ]
        extra_imports = imports.render()
        if extra_imports:
            lines.extend(extra_imports)
        if imports.enum_declarations:
            colliding_names = sorted(imports.imported_names() & set(imports.enum_names.values()))
            if colliding_names:
                raise ConfigurationError(
                    f"The migration imports {colliding_names} and declares an enum of the same name - "
                    "rename one of the enums"
                )
            lines.extend(["", *imports.enum_declarations])
        if imports.function_declarations:
            colliding_names = sorted(imports.imported_names() & set(imports.function_names.values()))
            if colliding_names:
                raise ConfigurationError(
                    f"The migration imports {colliding_names} and declares a function of the same name - rename "
                    "one of the functions"
                )
            for function_declaration in imports.function_declarations:
                lines.extend(["", "", function_declaration])
        lines.extend(["", "class Migration(migrations.Migration):"])
        blocks: list[list[str]] = []
        if self.dependencies:
            blocks.append([f"    dependencies = {self._render_dependencies(self.dependencies)}"])
        if self.run_before:
            blocks.append([f"    run_before = {self.run_before!r}"])
        if self.replaces:
            blocks.append([f"    replaces = {self.replaces!r}"])
        if self.initial is not None:
            blocks.append([f"    initial = {self.initial!r}"])
        if not self.atomic:
            blocks.append(["    atomic = False"])
        blocks.append(["    operations = [", *operations, "    ]"])
        for position, block in enumerate(blocks):
            lines.extend(block)
            if position < len(blocks) - 1:
                lines.append("")
        lines.append("")
        return "\n".join(lines)

    @staticmethod
    def _render_dependencies(dependencies: list[tuple[str, str]]) -> str:
        """Renders a migration's dependency list - a swappable one as
        ``migrations.swappable_dependency(setting)``.

        Args:
            dependencies: The (app_label, migration name) dependencies.
        """
        rendered_dependencies = [
            f"migrations.swappable_dependency({dependency.setting!r})"
            if isinstance(dependency, SwappableDependency)
            else repr(tuple(dependency))
            for dependency in dependencies
        ]
        return "[" + ", ".join(rendered_dependencies) + "]"

    def _format_operation(self, operation: Operation, imports: ImportManager, *, indent: str) -> list[str]:
        """An operation as source lines of the ``operations`` list - the call of its
        ``deconstruct()``: on one line when it fits, else ``ops.<Operation>(`` and each argument
        on a line of its own, a non-empty list one item per line.

        Args:
            operation: The operation.
            imports: Collects the imports the arguments need.
            indent: The indentation of the call.

        Returns:
            The lines.
        """
        path, args, kwargs = operation.deconstruct()
        operation_name = path.rsplit(".", 1)[1]
        if not any(isinstance(value, list) and value for value in kwargs.values()):
            arguments = [self.render_value(arg, imports) for arg in args]
            arguments += [f"{name}={self.render_value(value, imports)}" for name, value in kwargs.items()]
            line = f"{indent}ops.{operation_name}({', '.join(arguments)}),"
            if len(line) <= MIGRATION_LINE_LENGTH and "\n" not in line:
                return [line]
        lines = [f"{indent}ops.{operation_name}("]
        lines += [f"{indent}    {self.render_value(arg, imports)}," for arg in args]
        for name, value in kwargs.items():
            if isinstance(value, list) and value:
                lines.append(f"{indent}    {name}=[")
                lines += [f"{indent}        {self.render_value(item, imports)}," for item in value]
                lines.append(f"{indent}    ],")
            else:
                lines.append(f"{indent}    {name}={self.render_value(value, imports)},")
        lines.append(f"{indent}),")
        return lines
