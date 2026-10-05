from __future__ import annotations

import ast
import datetime as dt
import functools
import importlib
import inspect
import math
import subprocess  # nosec B404 - runs ruff only
import sys
import textwrap
import unicodedata
import uuid
import zoneinfo
from collections.abc import Iterable
from decimal import Decimal
from enum import Enum, Flag
from pathlib import Path
from typing import Any

from hare.classes.class_path import ClassPath
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
    MIGRATION_FORMATTER_RUFF_ARGUMENTS,
    MIGRATION_LINE_LENGTH,
    MIGRATION_SLUG_RE,
    MIGRATION_UNDERSCORE_RUN_PATTERN,
)
from hare.migrations.writer.import_manager import ImportManager
from hare.query.enums import Connector
from hare.query.expressions import Expression, F, Ordering, Q
from hare.sql.sql_context import NEUTRAL_SQL_CONTEXT


class MigrationWriter:
    """Writes a migration file of operations."""

    @staticmethod
    def slugify_name(value: str) -> str:
        slug = unicodedata.normalize("NFKC", value).strip().lower().replace(" ", "_")
        slug = MIGRATION_SLUG_RE.sub("_", slug)
        slug = MIGRATION_UNDERSCORE_RUN_PATTERN.sub("_", slug).strip("_")
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
        for name, obj in module.__dict__.items():
            if obj is value:
                return module_name, name, False
        qualname = getattr(value, "__qualname__", None)
        if qualname and "." in qualname:
            if "<locals>" in qualname:
                raise ConfigurationError(f"Cannot resolve import for {value!r}")
            return module_name, f"{module_name}.{qualname}", True
        name = getattr(value, "__name__", None)
        if name:
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
        imports.add_module("datetime")
        if isinstance(value, dt.datetime):
            base_class_name = "datetime"
        elif isinstance(value, dt.date):
            return f"datetime.date.fromisoformat({dt.date.isoformat(value)!r})"
        else:
            base_class_name = "time"
        tzinfo = value.tzinfo
        zone_key = getattr(tzinfo, "key", None)
        keeps_named_zone = isinstance(tzinfo, zoneinfo.ZoneInfo) and isinstance(zone_key, str)
        iso_text = (value.replace(tzinfo=None) if keeps_named_zone else value).isoformat()
        rendered = f"datetime.{base_class_name}.fromisoformat({iso_text!r})"
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
            imports.add_module(module_name)
        else:
            imports.add_from(module_name, name)
        return name

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
            # classes are callable too, so the class goes before the function
            MigrationWriter.render_class,
            MigrationWriter.render_function,
            MigrationWriter.render_self_describing_value,
        ):
            rendered = renderer(value, imports)
            if rendered is not None:
                return rendered
        return repr(value)

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
        if RuntimeEnums.is_importable(enum_class):
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
                imports.add_from("hare.fields.db_defaults", default_class.__name__)
                return f"{default_class.__name__}()"
        imports.add_from("hare.fields.db_defaults", "SqlDefault")
        return f"SqlDefault({value.sql!r})"

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
            imports.add_from("hare.query.expressions", "F")
            return f"F({value.name!r})"
        if isinstance(value, Ordering):
            imports.add_from("hare.query.expressions", "F")
            direction = "asc" if value.order.is_ascending else "desc"
            nulls = {True: "nulls_first=True", False: "nulls_last=True", None: ""}[value.order.nulls_first]
            return f"F({value.field_name!r}).{direction}({nulls})"
        if isinstance(value, OrderedIndexKey):
            imports.add_from("hare.ddl.indexes", "OrderedIndexKey")
            imports.add_from("hare.ddl", "RawSQLTerm")
            imports.add_from("hare.sql.enums", "Order")
            return (
                f"OrderedIndexKey(RawSQLTerm({value.term.get_sql(NEUTRAL_SQL_CONTEXT)!r}), Order.{value.order.name})"
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
            value, (Index, UniqueConstraint, CheckConstraint, ExclusionConstraint, ExclusiveArcCondition, Trigger)
        ):
            # A schema object renders its own SQL for a model - written as the call building it.
            object_path, object_args, object_kwargs = value.deconstruct()
            return MigrationWriter.render_call(object_path, object_args, object_kwargs, imports)
        if hasattr(value, "get_sql") and callable(value.get_sql):
            sql = value.get_sql(NEUTRAL_SQL_CONTEXT)
            imports.add_from("hare.ddl", "RawSQLTerm")
            return f"RawSQLTerm({sql!r})"
        return None

    @staticmethod
    def render_collection(value: Any, imports: ImportManager) -> str | None:
        """A list, a tuple or a dict as code.

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
        return None

    @staticmethod
    def render_standard_library_value(value: Any, imports: ImportManager) -> str | None:
        """A decimal, a UUID, a date, a time or a time difference as code.

        Args:
            value: The value.
            imports: Collects the imports the code needs.

        Returns:
            The code, None when the value is none of them.
        """
        if isinstance(value, Decimal):
            imports.add_from("decimal", "Decimal")
            return f"Decimal({str(value)!r})"
        if isinstance(value, uuid.UUID):
            imports.add_module("uuid")
            return f"uuid.UUID({str(value)!r})"
        if isinstance(value, dt.timedelta):
            imports.add_module("datetime")
            # Exact integer components - total_seconds() loses microseconds for a large timedelta.
            return (
                f"datetime.timedelta(days={value.days!r}, seconds={value.seconds!r}, "
                f"microseconds={value.microseconds!r})"
            )
        if isinstance(value, (dt.datetime, dt.date, dt.time)):
            return MigrationWriter.render_date_or_time(value, imports)
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
        imports.add_module("functools")
        return f"functools.partial({', '.join([function_reference, *arguments])})"

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
        if issubclass(value, Enum) and not RuntimeEnums.is_importable(value):
            return MigrationWriter.declare_enum(value, imports)
        return MigrationWriter.import_object(value, imports)

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
            imports.add_module(owner_class.__module__)
            return f"{owner_class.__module__}.{owner_class.__qualname__}.{value.__name__}"
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
            imports.add_from("hare.models", "swappable")
            return f"swappable({value.setting!r})"
        # `migration_import_path` imports an existing instance (a Crypto - the constructor would put the
        # key into the file).
        import_path = getattr(value, "migration_import_path", None)
        if import_path:
            module_name, _, name = import_path.rpartition(".")
            imports.add_from(module_name, name)
            return name
        # `deconstruct()` - for values that can be safely and fully rebuilt by calling the constructor
        # with literal arguments.
        if hasattr(value, "deconstruct") and callable(value.deconstruct):
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
        the top of the migration: its source, with the imports of its module it uses moved into
        its body, once per function.

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
        module_import_nodes: list[ast.stmt] = []
        module_defined_names: set[str] = set()
        for statement in ast.parse(module_source).body:
            if isinstance(statement, (ast.Import, ast.ImportFrom)):
                used_aliases = [
                    alias for alias in statement.names if (alias.asname or alias.name.split(".", 1)[0]) in used_names
                ]
                if used_aliases:
                    imported_statement = (
                        ast.Import(names=used_aliases)
                        if isinstance(statement, ast.Import)
                        else ast.ImportFrom(module=statement.module, names=used_aliases, level=statement.level)
                    )
                    module_import_nodes.append(imported_statement)
            elif isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                if statement.name != function.__name__:
                    module_defined_names.add(statement.name)
            elif isinstance(statement, (ast.Assign, ast.AnnAssign)):
                targets = statement.targets if isinstance(statement, ast.Assign) else [statement.target]
                module_defined_names.update(
                    node.id for target in targets for node in ast.walk(target) if isinstance(node, ast.Name)
                )
        if any(isinstance(node, ast.ImportFrom) and node.level for node in module_import_nodes):
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
            ConfigurationError: The condition holds an ``Exists(...)`` or an expression other than ``F()``.
        """
        if condition.expression is not None:
            raise ConfigurationError(f"{condition!r}: a Q(Exists(...)) condition can't be written into a migration")
        imports.add_from("hare.query.expressions", "Q")
        arguments = [MigrationWriter.render_condition(child, imports) for child in condition.children]
        for key, value in condition.filters.items():
            if isinstance(value, Expression) and type(value) is not F:
                raise ConfigurationError(f"{condition!r}: the value of {key!r} can't be written into a migration")
            arguments.append(f"{key}={MigrationWriter.render_value(value, imports)}")
        if condition.connector == Connector.OR:
            imports.add_from("hare.query.enums", "Connector")
            return f"{condition.get_negation_prefix()}Q.with_connector({', '.join(['Connector.OR', *arguments])})"
        return f"{condition.get_negation_prefix()}Q({', '.join(arguments)})"

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
    def get_callable(path: str) -> Any:
        """The object a ``render_call()`` expression for ``path`` calls once executed.

        Args:
            path: Dotted import path, as passed to ``render_call()``.

        Returns:
            The class or function the rendered call invokes.
        """
        if path.startswith("hare.fields."):
            return getattr(importlib.import_module("hare.fields"), MigrationWriter.get_public_field_name(path))
        module_name, name = path.rsplit(".", 1)
        return getattr(importlib.import_module(module_name), name)

    @staticmethod
    def render_call(path: str, args: list[Any], kwargs: dict[str, Any], imports: ImportManager) -> str:
        if path.startswith("hare.fields."):
            class_name = MigrationWriter.get_public_field_name(path)
            # For relational fields, move model_name to first positional arg
            if (
                path.rsplit(".", 1)[1].endswith("FieldInstance")
                and path.startswith("hare.fields.relations.fields.")
                and "model_name" in kwargs
            ):
                args = [kwargs.pop("model_name")] + list(args)
            imports.add_fields_alias()
            callee = f"fields.{class_name}"
        elif path.startswith("hare.ddl.indexes."):
            class_name = path.rsplit(".", 1)[1]
            imports.add_index_class(class_name)
            callee = class_name
        elif path == "hare.ddl.constraints.UniqueConstraint":
            imports.add_constraint_class("UniqueConstraint")
            callee = "UniqueConstraint"
        elif path == "hare.ddl.constraints.CheckConstraint":
            imports.add_constraint_class("CheckConstraint")
            callee = "CheckConstraint"
        elif path == "hare.ddl.constraints.ExclusionConstraint":
            imports.add_constraint_class("ExclusionConstraint")
            callee = "ExclusionConstraint"
        else:
            module, name = path.rsplit(".", 1)
            imports.add_from(module, name)
            callee = name

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
