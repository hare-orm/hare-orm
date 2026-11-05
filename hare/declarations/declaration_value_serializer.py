from __future__ import annotations
import __future__

import ast
import datetime as dt
import functools
import importlib
import inspect
import ipaddress
import math
import sys
import textwrap
import types
import typing
import uuid
import zoneinfo
from decimal import Decimal
from enum import Enum, Flag
from typing import Any

from hare.classes.class_path import ClassPath
from hare.classes.comparable_values import ComparableValues
from hare.classes.constants import FUNCTION_DECLARATION_PATH_ATTRIBUTE
from hare.ddl.indexes.ordered_index_key import OrderedIndexKey
from hare.declarations.constants import (
    DIALECT_FIELDS_PACKAGE_SEPARATOR,
)
from hare.declarations.global_reference_rewriter import GlobalReferenceRewriter
from hare.declarations.import_manager import ImportManager
from hare.declarations.runtime_enums import RuntimeEnums
from hare.exceptions import ConfigurationError
from hare.fields.db_defaults.now import Now
from hare.fields.db_defaults.random_hex import RandomHex
from hare.fields.db_defaults.sql_default import SqlDefault
from hare.fields.db_defaults.uuid_v7 import UuidV7
from hare.fields.relations.swappable_model_reference import SwappableModelReference
from hare.query.enums import Connector
from hare.query.expressions import Expression, F, Ordering, Q
from hare.sql.constants import NEUTRAL_SQL_CONTEXT


class DeclarationValueSerializer:
    """Render declaration values and gather imports independently of a migration file."""

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
            zone_class_name = DeclarationValueSerializer.import_object(type(tzinfo), imports)
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
        module_name, name, use_module = DeclarationValueSerializer._get_import(value)
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
            DeclarationValueSerializer.render_enum_member,
            DeclarationValueSerializer.render_literal,
            DeclarationValueSerializer.render_database_default,
            DeclarationValueSerializer.render_query_expression,
            DeclarationValueSerializer.render_schema_object,
            DeclarationValueSerializer.render_collection,
            DeclarationValueSerializer.render_standard_library_value,
            DeclarationValueSerializer.render_partial,
            # a parameterized type passes for its class (isinstance(dict[str, int], type)), so it goes first
            DeclarationValueSerializer.render_type_expression,
            # classes are callable too, so the class goes before the function
            DeclarationValueSerializer.render_class,
            DeclarationValueSerializer.render_function,
        ):
            rendered = renderer(value, imports)
            if rendered is not None:
                return rendered
        return DeclarationValueSerializer.render_literal_text(value)

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
        if DeclarationValueSerializer.is_imported_enum(enum_class, imports):
            name = DeclarationValueSerializer.import_object(enum_class, imports)
        else:
            name = DeclarationValueSerializer.declare_enum(enum_class, imports)
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
            and DeclarationValueSerializer.is_importable_module_name(module_name)
            and class_name.isidentifier()
        ):
            # A subclass keeps its class - a dialect's renderer of it, its own SQL.
            return DeclarationValueSerializer.render_call(path, args, kwargs, imports)
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
            return DeclarationValueSerializer.render_condition(value, imports)
        if type(value) is F:
            return f"{imports.add_from('hare.query.expressions', 'F')}({value.name!r})"
        if isinstance(value, Ordering):
            direction = "asc" if value.order.is_ascending else "desc"
            nulls = {True: "nulls_first=True", False: "nulls_last=True", None: ""}[value.order.nulls_first]
            if isinstance(value.target, str):
                return f"{imports.add_from('hare.query.expressions', 'F')}({value.target!r}).{direction}({nulls})"
            return f"{DeclarationValueSerializer.render_query_expression(value.target, imports)}.{direction}({nulls})"
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
            return DeclarationValueSerializer.render_call(expression_path, expression_args, expression_kwargs, imports)
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
        if isinstance(value, type):
            return None
        # Constructor declarations take precedence over SQL renderers, which may
        # require runtime context or evaluate an environment reference.
        declaration = DeclarationValueSerializer.render_self_describing_value(value, imports)
        if declaration is not None:
            return declaration
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
            return "[" + ", ".join(DeclarationValueSerializer.render_value(item, imports) for item in value) + "]"
        if isinstance(value, tuple):
            if len(value) == 1:
                return f"({DeclarationValueSerializer.render_value(value[0], imports)},)"
            return "(" + ", ".join(DeclarationValueSerializer.render_value(item, imports) for item in value) + ")"
        if isinstance(value, dict):
            items = [
                f"{DeclarationValueSerializer.render_value(key, imports)}: "
                f"{DeclarationValueSerializer.render_value(item_value, imports)}"
                for key, item_value in value.items()
            ]
            return "{" + ", ".join(items) + "}"
        if isinstance(value, (set, frozenset)):
            # Sorted by their code - a set has no order, a file written twice is the same.
            items_code = sorted(DeclarationValueSerializer.render_value(item, imports) for item in value)
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
            return DeclarationValueSerializer.render_date_or_time(value, imports)
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
        arguments = [DeclarationValueSerializer.render_value(argument, imports) for argument in value.args]
        arguments += [
            f"{key}={DeclarationValueSerializer.render_value(item_value, imports)}"
            for key, item_value in value.keywords.items()
        ]
        function_reference = DeclarationValueSerializer.render_value(function, imports)
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
            return " | ".join(
                DeclarationValueSerializer.render_type_argument(argument, imports) for argument in arguments
            )
        typing_name = DeclarationValueSerializer.get_typing_name(value, origin)
        if typing_name is not None:
            # A typing alias (List, Dict) or a special form (Literal, Annotated) - not equal to the
            # builtin's own parameterized type, so written by its typing name.
            origin_text = f"{imports.add_module('typing')}.{typing_name}"
        else:
            origin_text = DeclarationValueSerializer.render_value(origin, imports)
        if not arguments:
            return f"{origin_text}[()]"
        argument_texts = [DeclarationValueSerializer.render_type_argument(argument, imports) for argument in arguments]
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
        return DeclarationValueSerializer.render_value(value, imports)

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
        if issubclass(value, Enum) and not DeclarationValueSerializer.is_imported_enum(value, imports):
            return DeclarationValueSerializer.declare_enum(value, imports)
        return DeclarationValueSerializer.import_object(value, imports)

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
        if inspect.isfunction(value) and not DeclarationValueSerializer.is_importable_module_name(value.__module__):
            return DeclarationValueSerializer.declare_function(value, imports)
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
        return DeclarationValueSerializer.import_object(value, imports)

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
            return DeclarationValueSerializer.render_call(path, args, kwargs, imports)
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
        content = ComparableValues.get_enum_content(enum_type)
        name = imports.enum_names.get(content)
        if name is not None:
            return name
        enum_base, data_type = RuntimeEnums.get_bases(enum_type)
        base_source = DeclarationValueSerializer.render_value(enum_base, imports)
        members_source = ", ".join(
            f"{member_name!r}: {DeclarationValueSerializer.render_value(member.value, imports)}"
            for member_name, member in enum_type.__members__.items()
        )
        type_source = (
            "" if data_type is None else f", type={DeclarationValueSerializer.render_value(data_type, imports)}"
        )
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
        the top of the migration, together with its module imports. References are rebound
        without capturing local names; each function is copied once.

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
        used_names = GlobalReferenceRewriter.get_global_names(function_source)
        postponed_annotations = bool(inspect.unwrap(function).__code__.co_flags & __future__.annotations.compiler_flag)
        runtime_names = (
            GlobalReferenceRewriter.get_global_names(
                DeclarationValueSerializer.postpone_function_annotations(function_source)
            )
            if postponed_annotations
            else used_names
        )
        local_names = GlobalReferenceRewriter.get_local_names(function_source)
        module_import_nodes: list[ast.Import | ast.ImportFrom] = []
        module_defined_names: set[str] = set()
        for statement in ast.parse(module_source).body:
            if isinstance(statement, (ast.Import, ast.ImportFrom)):
                aliases = [
                    alias
                    for alias in statement.names
                    if (alias.asname or alias.name.split(".", 1)[0]) in used_names
                    and (alias.asname or alias.name.split(".", 1)[0]) != function.__name__
                ]
                if aliases:
                    module_import_nodes.append(
                        ast.Import(names=aliases)
                        if isinstance(statement, ast.Import)
                        else ast.ImportFrom(module=statement.module, names=aliases, level=statement.level)
                    )
            elif isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                if statement.name != function.__name__:
                    module_defined_names.add(statement.name)
            elif isinstance(statement, (ast.Assign, ast.AnnAssign)):
                targets = statement.targets if isinstance(statement, ast.Assign) else [statement.target]
                module_defined_names.update(
                    node.id
                    for target in targets
                    for node in ast.walk(target)
                    if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store)
                )
        if any(isinstance(node, ast.ImportFrom) and node.level for node in module_import_nodes):
            raise ConfigurationError(
                f"{function.__module__}.{function.__qualname__} uses a relative import - write it as an absolute "
                "one to copy the function into the migration"
            )
        missing_names = sorted(runtime_names & module_defined_names)
        if missing_names:
            raise ConfigurationError(
                f"{function.__module__}.{function.__qualname__} uses {', '.join(missing_names)} of its module, which "
                "a copy of the function in another migration wouldn't have - move them into the function or into "
                "an importable module"
            )
        replacements: dict[str, str] = {}
        for statement in module_import_nodes:
            for alias in statement.names:
                if isinstance(statement, ast.ImportFrom):
                    replacements[alias.asname or alias.name] = imports.add_scoped_import(
                        statement.module or "", alias.name, local_names
                    )
                elif alias.asname:
                    replacements[alias.asname] = imports.add_scoped_import(alias.name, None, local_names)
                else:
                    # Importing the child also loads it onto the root package. References
                    # may use that root itself or another already imported child.
                    imports.add_module(alias.name)
                    root = alias.name.split(".", 1)[0]
                    replacements[root] = imports.add_scoped_import(root, None, local_names)
        name = imports.get_free_name(function.__name__, excluded_names=local_names)
        function_node.name = name
        replacements[function.__name__] = name
        source = GlobalReferenceRewriter.rewrite(ast.unparse(function_node), replacements)
        if postponed_annotations:
            source = DeclarationValueSerializer.postpone_function_annotations(source)
        imports.function_names[function_key] = name
        declaration_path = getattr(function, FUNCTION_DECLARATION_PATH_ATTRIBUTE, None) or ClassPath.get(function)
        imports.function_declarations.append(
            f"{source}\n{name}.{FUNCTION_DECLARATION_PATH_ATTRIBUTE} = {declaration_path!r}"
        )
        return name

    @staticmethod
    def postpone_function_annotations(source: str) -> str:
        """Keep a copied function's postponed annotations without changing the target module.

        String annotations preserve the source's deferred evaluation on Python versions
        with eager annotations too. Nested definitions share the source future flag.
        Runtime defaults, decorators and bodies remain ordinary expressions.
        """
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, (ast.arg, ast.AnnAssign)) and node.annotation is not None:
                node.annotation = ast.Constant(ast.unparse(node.annotation))
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.returns is not None:
                node.returns = ast.Constant(ast.unparse(node.returns))
        return ast.unparse(tree)

    @staticmethod
    def render_condition(condition: Q, imports: ImportManager) -> str:
        """A ``Q`` condition as the code rebuilding it.

        Args:
            condition: The condition.
            imports: Collects the imports the code needs.

        Returns:
            The code, e.g. ``~Q(qty__lt=0) | Q(price__isnull=True)`` as ``Q(~Q(qty__lt=0), ...)``.

        Raises:
            ConfigurationError: The condition holds an expression predicate, including ``Exists(...)``.
        """
        if condition.expression is not None:
            raise ConfigurationError(f"{condition!r}: a Q expression predicate can't be written into a migration")
        q_name = imports.add_from("hare.query.expressions", "Q")
        arguments = [DeclarationValueSerializer.render_condition(child, imports) for child in condition.children]
        for key, value in condition.filters.items():
            # An expression (F("low") + 1) is written as itself, as an index key is.
            arguments.append(f"{key}={DeclarationValueSerializer.render_value(value, imports)}")
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
        return package_name if hasattr(package, DeclarationValueSerializer.get_public_field_name(path)) else None

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
            importlib.import_module("hare.fields"), DeclarationValueSerializer.get_public_field_name(path), None
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
        if path.startswith("hare.fields.") and DeclarationValueSerializer.is_fields_package_path(path):
            return getattr(
                importlib.import_module("hare.fields"), DeclarationValueSerializer.get_public_field_name(path)
            )
        module_name, name, use_module = DeclarationValueSerializer._split_import_path(path)
        value = importlib.import_module(module_name)
        if use_module:
            for part in name.split("."):
                value = getattr(value, part)
            return value
        return getattr(value, name)

    @staticmethod
    def get_import_reference(path: str, imports: ImportManager) -> str:
        """Bind an import path once, preserving nested classes and name collisions."""
        module, name, use_module = DeclarationValueSerializer._split_import_path(path)
        return f"{imports.add_module(module)}.{name}" if use_module else imports.add_from(module, name)

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
        if path.startswith("hare.fields.") and DeclarationValueSerializer.is_fields_package_path(path):
            class_name = DeclarationValueSerializer.get_public_field_name(path)
            # For relational fields, move model_name to first positional arg
            if (
                path.rsplit(".", 1)[1].endswith("FieldInstance")
                and path.startswith("hare.fields.relations.fields.")
                and "model_name" in kwargs
            ):
                args = [kwargs["model_name"], *args]
                kwargs = {name: value for name, value in kwargs.items() if name != "model_name"}
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
            if (public_field_module := DeclarationValueSerializer.get_public_field_module(path)) is not None:
                callee = imports.add_from(public_field_module, DeclarationValueSerializer.get_public_field_name(path))
            else:
                callee = DeclarationValueSerializer.get_import_reference(path, imports)

        rendered_args = [DeclarationValueSerializer.render_value(arg, imports) for arg in args]
        rendered_kwargs = [
            f"{key}={DeclarationValueSerializer.render_value(argument_value, imports)}"
            for key, argument_value in kwargs.items()
        ]
        return f"{callee}({', '.join(rendered_args + rendered_kwargs)})"
