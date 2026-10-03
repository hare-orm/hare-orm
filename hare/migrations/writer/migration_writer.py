import datetime as dt
import functools
import importlib
import inspect
import math
import re
import subprocess  # nosec B404 - runs ruff only
import unicodedata
import uuid
import zoneinfo
from collections.abc import Iterable
from decimal import Decimal
from enum import Enum, Flag
from pathlib import Path
from typing import Any

from hare.ddl.constraints.check_constraint import CheckConstraint
from hare.ddl.constraints.exclusion_constraint import ExclusionConstraint
from hare.ddl.constraints.unique_constraint import UniqueConstraint
from hare.ddl.indexes.index import Index
from hare.ddl.indexes.ordered_index_key import OrderedIndexKey
from hare.ddl.triggers import Trigger
from hare.exceptions import ConfigurationError
from hare.fields.db_defaults.now import Now
from hare.fields.db_defaults.random_hex import RandomHex
from hare.fields.db_defaults.sql_default import SqlDefault
from hare.fields.swappable import SwappableModelReference
from hare.migrations.constants import (
    MIGRATION_FORMATTER_RUFF_ARGUMENTS,
    MIGRATION_LINE_LENGTH,
    MIGRATION_SLUG_RE,
)
from hare.migrations.operations import Operation
from hare.migrations.runtime_enums import RuntimeEnums
from hare.migrations.swappable import SwappableDependency
from hare.migrations.writer.import_manager import ImportManager
from hare.query.enums import Connector
from hare.query.expressions import Expression, F, Ordering, Q
from hare.sql.context import NEUTRAL_SQL_CONTEXT
from hare.utils.class_path import ClassPath


class MigrationWriter:
    @staticmethod
    def slugify_name(value: str) -> str:
        slug = unicodedata.normalize("NFKC", value).strip().lower().replace(" ", "_")
        slug = MIGRATION_SLUG_RE.sub("_", slug)
        slug = re.sub(r"_+", "_", slug).strip("_")
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
            module_name, zone_class_name, use_module = MigrationWriter._get_import(type(tzinfo))
            if use_module:
                imports.add_module(module_name)
            else:
                imports.add_from(module_name, zone_class_name)
            replacements.append(f"tzinfo={zone_class_name}({zone_key!r})")
        if value.fold:
            replacements.append(f"fold={value.fold!r}")
        if replacements:
            rendered += f".replace({', '.join(replacements)})"
        return rendered

    @staticmethod
    def render_value(value: Any, imports: ImportManager) -> str:
        # StrEnum instances are both str and Enum, so check Enum first
        if isinstance(value, Enum):
            enum_cls = value.__class__
            if RuntimeEnums.is_importable(enum_cls):
                module_name, name, use_module = MigrationWriter._get_import(enum_cls)
                if use_module:
                    imports.add_module(module_name)
                else:
                    imports.add_from(module_name, name)
            else:
                name = MigrationWriter.declare_enum(enum_cls, imports)
            if isinstance(value, Flag) and value.name not in enum_cls.__members__:
                # A combination of flags (or an empty one) has no member of its own to name.
                return f"{name}({value.value!r})"
            return f"{name}.{value.name}"
        if isinstance(value, SwappableModelReference):
            imports.add_from("hare.models", "swappable")
            return f"swappable({value.setting!r})"
        if isinstance(value, float) and not math.isfinite(value):
            return f"float({str(value)!r})"
        if value is None or isinstance(value, (bool, int, float, str)):
            return repr(value)
        if isinstance(value, bytes):
            return repr(value)
        if isinstance(value, Now):
            imports.add_from("hare.fields.db_defaults", "Now")
            return "Now()"
        if isinstance(value, RandomHex):
            imports.add_from("hare.fields.db_defaults", "RandomHex")
            return "RandomHex()"
        if isinstance(value, SqlDefault):
            imports.add_from("hare.fields.db_defaults", "SqlDefault")
            return f"SqlDefault({value.sql!r})"
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
        if isinstance(value, (Index, UniqueConstraint, CheckConstraint, ExclusionConstraint, Trigger)):
            # A schema object renders its own SQL for a model - written as the call building it.
            object_path, object_args, object_kwargs = value.deconstruct()
            return MigrationWriter.render_call(object_path, object_args, object_kwargs, imports)
        if hasattr(value, "get_sql") and callable(value.get_sql):
            sql = value.get_sql(NEUTRAL_SQL_CONTEXT)
            imports.add_from("hare.ddl", "RawSQLTerm")
            return f"RawSQLTerm({sql!r})"
        if isinstance(value, list):
            return "[" + ", ".join(MigrationWriter.render_value(item, imports) for item in value) + "]"
        if isinstance(value, tuple):
            if len(value) == 1:
                return f"({MigrationWriter.render_value(value[0], imports)},)"
            return "(" + ", ".join(MigrationWriter.render_value(item, imports) for item in value) + ")"
        if isinstance(value, dict):
            items = [
                f"{MigrationWriter.render_value(key, imports)}: {MigrationWriter.render_value(val, imports)}"
                for key, val in value.items()
            ]
            return "{" + ", ".join(items) + "}"
        if isinstance(value, Decimal):
            imports.add_from("decimal", "Decimal")
            return f"Decimal({str(value)!r})"
        if isinstance(value, uuid.UUID):
            imports.add_module("uuid")
            return f"uuid.UUID({str(value)!r})"
        if isinstance(value, (dt.datetime, dt.date, dt.time, dt.timedelta)):
            imports.add_module("datetime")
            if isinstance(value, dt.timedelta):
                # Exact integer components - total_seconds() loses microseconds for a large
                # timedelta.
                return (
                    f"datetime.timedelta(days={value.days!r}, seconds={value.seconds!r}, "
                    f"microseconds={value.microseconds!r})"
                )
            return MigrationWriter.render_date_or_time(value, imports)
        if isinstance(value, functools.partial):
            func = value.func
            if getattr(func, "__name__", "") == "<lambda>":
                raise ConfigurationError(
                    "Cannot serialize lambda inside functools.partial; use a module-level function."
                )
            if hasattr(func, "__qualname__") and "<locals>" in func.__qualname__:
                raise ConfigurationError(f"Cannot serialize partial with local function: {func!r}")
            args = ", ".join(MigrationWriter.render_value(arg, imports) for arg in value.args)
            kwargs = (
                ", ".join(f"{key}={MigrationWriter.render_value(val, imports)}" for key, val in value.keywords.items())
                if value.keywords
                else ""
            )
            parts = ", ".join(part for part in (args, kwargs) if part)
            module_name, name, use_module = MigrationWriter._get_import(func)
            if use_module:
                imports.add_module(module_name)
                func_ref = name
            else:
                imports.add_from(module_name, name)
                func_ref = name
            imports.add_module("functools")
            return f"functools.partial({', '.join([func_ref, parts]) if parts else func_ref})"
        # Check type before inspect.isfunction (classes are callable too)
        if isinstance(value, type):
            if value.__module__ == "builtins":
                return value.__name__
            if issubclass(value, Enum) and not RuntimeEnums.is_importable(value):
                return MigrationWriter.declare_enum(value, imports)
            module_name, name, use_module = MigrationWriter._get_import(value)
            if use_module:
                imports.add_module(module_name)
                return name
            imports.add_from(module_name, name)
            return name
        if inspect.isfunction(value) or callable(value):
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
            module_name, name, use_module = MigrationWriter._get_import(value)
            if use_module:
                imports.add_module(module_name)
                return name
            imports.add_from(module_name, name)
            return name
        # An object in a field's kwargs says how it is written: `migration_import_path` imports an
        # existing instance (a Crypto - the constructor would put the key into the file).
        import_path = getattr(value, "migration_import_path", None)
        if import_path:
            module_name, _, name = import_path.rpartition(".")
            imports.add_from(module_name, name)
            return name
        # 2. `.deconstruct()` (the same protocol Field/Index/Constraint already use) - for values that
        #    can be safely and fully rebuilt by calling the constructor with literal args.
        if hasattr(value, "deconstruct") and callable(value.deconstruct):
            path, args, kwargs = value.deconstruct()
            return MigrationWriter.render_call(path, args, kwargs, imports)
        return repr(value)

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
        rendered_kwargs = [f"{key}={MigrationWriter.render_value(val, imports)}" for key, val in kwargs.items()]
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
    ) -> None:
        self.name = name
        self.app_label = app_label
        self.operations = list(operations)
        self.dependencies = dependencies or []
        self.run_before = run_before or []
        self.replaces = replaces or []
        self.initial = initial
        self.migrations_module = migrations_module

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
        blocks.append(["    operations = [", *operations, "    ]"])
        for idx, block in enumerate(blocks):
            lines.extend(block)
            if idx < len(blocks) - 1:
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
