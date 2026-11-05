from __future__ import annotations

import collections.abc
import datetime
import functools
import importlib.util
import inspect
import math
import sys
import types
import typing
from decimal import Decimal
from enum import IntFlag

import pytest

from hare.declarations.declaration_value_serializer import DeclarationValueSerializer
from hare.declarations.declaration_values import DeclarationValues
from hare.declarations.environment_value import EnvironmentValue
from hare.declarations.import_manager import ImportManager
from hare.dialects.clickhouse.schema_objects.dictionary_source import DictionarySource
from hare.dialects.dialect_registry import DialectRegistry
from hare.exceptions import ConfigurationError
from hare.migrations.operations import RunPython
from hare.migrations.writer import MigrationWriter

pytestmark = pytest.mark.database_independent


class Permission(IntFlag):
    READ = 1
    WRITE = 2
    EXECUTE = 4


Item = typing.TypeVar("Item")


class Box(typing.Generic[Item]):
    pass


type Pairs[Key] = dict[Key, int]

# A class kept in its module under a name no import statement takes - as pydantic keeps the class of
# GenericModel[int].
globals()["Box[int]"] = type("Box[int]", (), {"__module__": __name__})


def render_and_evaluate(value: object) -> object:
    """Renders `value` the way a migration file would, then evaluates that source."""
    imports = ImportManager()
    source = DeclarationValueSerializer.render_value(value, imports)
    namespace: dict[str, object] = {}
    exec("\n".join(imports.render()), namespace)  # noqa: S102
    return eval(source, namespace)  # noqa: S307


def migration_value(value=Decimal("1.25")):
    return value


def test_environment_source_serializes_its_reference_and_only_runtime_sql_reads_its_value(monkeypatch):
    name = "HARE_TEST_DECLARATION_PASSWORD"
    monkeypatch.delenv(name, raising=False)
    source = DictionarySource("CLICKHOUSE", table="books", password=EnvironmentValue(name))
    restored = render_and_evaluate(source)
    assert restored == source
    literals = DialectRegistry.get_dialect("clickhouse").literals
    assert restored.get_sql(literals, preview=True) == "CLICKHOUSE(TABLE 'books' PASSWORD '[HIDDEN]')"
    with pytest.raises(ConfigurationError, match=name):
        restored.get_sql(literals)
    monkeypatch.setenv(name, "first")
    assert restored.get_sql(literals) == "CLICKHOUSE(TABLE 'books' PASSWORD 'first')"
    monkeypatch.setenv(name, "second")
    assert restored.get_sql(literals) == "CLICKHOUSE(TABLE 'books' PASSWORD 'second')"
    rendered = DeclarationValueSerializer.render_value(restored, ImportManager())
    assert name in rendered and "second" not in rendered


@pytest.mark.parametrize(
    "value",
    [
        Permission.READ,
        Permission.READ | Permission.WRITE,
        Permission.READ | Permission.WRITE | Permission.EXECUTE,
        Permission(0),
    ],
)
def test_flag_value_renders_to_source_that_rebuilds_the_same_value(value: Permission) -> None:
    rebuilt = render_and_evaluate(value)

    assert rebuilt == value
    assert type(rebuilt) is Permission


@pytest.mark.parametrize("value", [math.inf, -math.inf])
def test_infinite_float_renders_to_source_that_rebuilds_the_same_value(value: float) -> None:
    assert render_and_evaluate(value) == value


def test_nan_float_renders_to_source_that_rebuilds_nan() -> None:
    rebuilt = render_and_evaluate(math.nan)

    assert isinstance(rebuilt, float)
    assert math.isnan(rebuilt)


@pytest.mark.parametrize(
    "value", [datetime.date.today, datetime.datetime.now, datetime.datetime.utcnow, datetime.datetime.today]
)
def test_builtin_classmethod_renders_to_source_that_rebuilds_the_same_callable(value: object) -> None:
    assert render_and_evaluate(value) == value


def test_builtin_method_bound_to_an_instance_raises_a_clear_error() -> None:
    with pytest.raises(ConfigurationError, match="Cannot resolve import"):
        DeclarationValueSerializer.render_value([].append, ImportManager())


@pytest.mark.database_independent
@pytest.mark.parametrize(
    ("path", "expected_call", "expected_class"),
    [
        pytest.param(
            "hare.fields.data.json.json_path_field.JSONPathField",
            "JSONPathField()",
            "hare.fields.data.json.json_path_field.JSONPathField",
            id="class_hare_fields_doesnt_export",
        ),
    ],
)
def test_a_field_class_is_written_as_the_name_its_migration_reads(path, expected_call, expected_class) -> None:
    """A field class of hare.fields the package doesn't export is written by its own module - as fields.<name>
    the migration couldn't be read. The exported classes are written and read back by the field matrix."""
    imports = ImportManager()
    source = DeclarationValueSerializer.render_call(path, [], {}, imports)
    namespace: dict[str, object] = {}
    exec("\n".join(imports.render()), namespace)  # noqa: S102
    called = eval(source.removesuffix("()"), namespace)  # noqa: S307
    assert source == expected_call
    assert f"{called.__module__}.{called.__qualname__}" == expected_class
    assert DeclarationValueSerializer.get_callable(path) is called


@pytest.mark.parametrize(
    "value",
    [
        dict[str, str],
        list[str],
        dict[str, list[int]],
        tuple[int, ...],
        tuple[()],
        int | None,
        str | int | None,
        typing.Literal["a", "b"],
        typing.Optional[str],
        collections.abc.Mapping[str, datetime.date],
        list[Permission],
        typing.List[int],  # noqa: UP006
        typing.Dict[str, list[int]],  # noqa: UP006
        typing.Tuple[int, ...],  # noqa: UP006
        typing.Callable[[int], str],  # noqa: UP006
        Box[int],
    ],
    ids=repr,
)
def test_a_parameterized_type_renders_to_source_that_rebuilds_it(value: object) -> None:
    """A JSONField's field_type=dict[str, str] was written as the bare dict - the migration read back
    another type and makemigrations refused to write it."""
    restored = render_and_evaluate(value)
    assert restored == value
    assert DeclarationValues.are_equal(value, restored)


@pytest.mark.parametrize(
    "value", [Pairs[str], globals()["Box[int]"]], ids=["type_alias_statement", "class_named_by_no_identifier"]
)
def test_a_type_a_migration_cant_import_is_refused(value: object) -> None:
    with pytest.raises(ConfigurationError):
        DeclarationValueSerializer.render_value(value, ImportManager())


def test_copied_function_imports_default_values_before_its_declaration() -> None:
    imports = ImportManager()
    name = DeclarationValueSerializer.declare_function(migration_value, imports)
    namespace: dict[str, object] = {}
    exec(
        compile("\n".join([*imports.render(), *imports.function_declarations]), "copied", "exec", dont_inherit=True),
        namespace,
    )  # noqa: S102
    assert namespace[name]() == Decimal("1.25")
    assert DeclarationValues.are_equal(migration_value, namespace[name])


def test_functions_with_the_same_name_in_different_modules_compare_separately():
    other = types.FunctionType(migration_value.__code__, migration_value.__globals__, migration_value.__name__)
    other.__module__ = "another_module"
    assert not DeclarationValues.are_equal(migration_value, other)


@pytest.mark.parametrize(
    ("first", "second"),
    [
        ({1: "value"}, {"1": "value"}),
        (Decimal("1"), "1"),
        (True, 1),
        (b"value", "b'value'"),
        (float("nan"), ["float", "nan"]),
        ({1, 2}, [1, 2]),
    ],
)
def test_declaration_comparison_retains_value_and_mapping_key_types(first, second):
    assert not DeclarationValues.are_equal(first, second)
    assert DeclarationValues.are_equal(first, render_and_evaluate(first))
    assert DeclarationValues.are_equal(second, render_and_evaluate(second))


def test_unsupported_declaration_does_not_compare_by_its_display_text():
    class DisplayedValue:
        def __str__(self):
            pytest.fail("declaration comparison called arbitrary __str__")

    with pytest.raises(ConfigurationError, match="provide deconstruct"):
        DeclarationValues.get(DisplayedValue())


@pytest.mark.parametrize(
    "declaration", ["annotations-and-defaults", "decorator", "lambda-local", "comprehension-local"]
)
def test_copied_function_rebinds_import_aliases_at_expression_roots(tmp_path, monkeypatch, declaration):
    if declaration == "decorator":
        source = "from contextlib import contextmanager as manage\n@manage\ndef copied():\n    yield 7\n"
    elif declaration in ("lambda-local", "comprehension-local"):
        expression = (
            "(lambda circle: circle)(2)" if declaration == "lambda-local" else "[circle * 2 for circle in (1, 2)]"
        )
        source = f"from math import pi as circle\ndef copied(value={expression}):\n    return value\n"
    else:
        source = (
            "from datetime import date as calendar_date\n"
            "from math import pi as circle\n"
            "def copied(value: calendar_date = calendar_date(2026, 1, 1), *, scale=circle) -> calendar_date:\n"
            "    return value, scale\n"
        )
    path = tmp_path / "copied_function.py"
    path.write_text("date = pi = contextmanager = None\n" + source, encoding="utf-8")
    specification = importlib.util.spec_from_file_location("copied_function_for_migration", path)
    module = importlib.util.module_from_spec(specification)
    monkeypatch.setitem(sys.modules, specification.name, module)
    specification.loader.exec_module(module)
    imports = ImportManager()
    # Force a second spelling of each imported name, as another declaration may already use it.
    for imported_name in ("date", "pi", "contextmanager"):
        imports.add_from(specification.name, imported_name)
    name = DeclarationValueSerializer.declare_function(module.copied, imports)
    namespace = {}
    exec("\n".join([*imports.render(), *imports.function_declarations]), namespace)  # noqa: S102
    copied = namespace[name]
    if declaration == "decorator":
        with copied() as value:
            assert value == 7
    elif declaration in ("lambda-local", "comprehension-local"):
        assert copied() == module.copied()
    else:
        assert copied() == (datetime.date(2026, 1, 1), math.pi)
        assert typing.get_type_hints(copied) == {"value": datetime.date, "return": datetime.date}


@pytest.mark.parametrize("partial", [False, True])
def test_copied_callback_preserves_postponed_annotations(tmp_path, monkeypatch, partial):
    source = (
        "from __future__ import annotations\n"
        "from typing import TYPE_CHECKING\n"
        "if TYPE_CHECKING:\n"
        "    from missing_types import MissingType\n"
        "class AnnotationOnly:\n"
        "    pass\n"
        "def copied(apps: MissingType, editor: AnnotationOnly, amount=7) -> MissingType:\n"
        "    return amount\n"
    )
    path = tmp_path / "0001_initial.py"
    path.write_text(source, encoding="utf-8")
    specification = importlib.util.spec_from_file_location("migration_callbacks.0001_initial", path)
    module = importlib.util.module_from_spec(specification)
    monkeypatch.setitem(sys.modules, specification.name, module)
    specification.loader.exec_module(module)
    callback = functools.partial(module.copied, amount=9) if partial else module.copied
    generated = MigrationWriter("0001_squashed", "books", [RunPython(callback)]).as_string()
    namespace = {}
    exec(compile(generated, "generated_migration", "exec", dont_inherit=True), namespace)  # noqa: S102
    restored = namespace["Migration"].operations[0].code
    assert restored(None, None) == (9 if partial else 7)
    function = restored.func if partial else restored
    assert function.__annotations__ == module.copied.__annotations__


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "source,expected",
    [
        ("def copied(value=4):\n    return value + copied(value-1) if value else 0\n", 10),
        ("def copied(value=4):\n    global copied\n    return value + copied(value-1) if value else 0\n", 10),
        ("shadow=99\ndef copied(shadow=4):\n    return shadow\n", 4),
        ("from math import pi as circle\ndef copied(circle=circle):\n    return circle\n", math.pi),
        (
            "def copied(value=4):\n    def nested(copied):\n        return copied\n"
            "    return copied(value-1) + nested(value) if value else 0\n",
            10,
        ),
        ("async def copied(value=4):\n    return value + await copied(value-1) if value else 0\n", 10),
        ("from math import pi\ndef copied():\n    global pi\n    return pi\n", math.pi),
        (
            "from math import pi\ndef copied():\n    def inner():\n        global pi\n        return pi\n"
            "    return inner()\n",
            math.pi,
        ),
        ("from math import pi as circle\ndef copied(pi=1, pi2=2):\n    return circle + pi + pi2\n", math.pi + 3),
        (
            "def copied(value=4, copied2=99):\n    return value + copied(value-1) if value else 0\n",
            10,
        ),
        (
            "from math import pi as circle\ndef copied():\n    return [pi + circle for pi in (1, 2)]\n",
            [math.pi + 1, math.pi + 2],
        ),
        ("import os.path\ndef copied():\n    return os.path.basename('a/b')\n", "b"),
        ("import os.path as paths\ndef copied():\n    global paths\n    return paths.basename('a/b')\n", "b"),
    ],
    ids=[
        "recursive",
        "explicit-global",
        "local-shadow",
        "default-import",
        "nested-shadow",
        "async-recursive",
        "global-import",
        "nested-global-import",
        "import-alias-capture",
        "recursive-alias-capture",
        "comprehension-import",
        "module-root",
        "module-alias",
    ],
)
async def test_copied_functions_keep_bindings_when_name_is_reserved(tmp_path, monkeypatch, source, expected):
    path = tmp_path / "function_source.py"
    path.write_text(source, encoding="utf-8")
    specification = importlib.util.spec_from_file_location("function_bindings_for_migration", path)
    module = importlib.util.module_from_spec(specification)
    monkeypatch.setitem(sys.modules, specification.name, module)
    specification.loader.exec_module(module)
    imports = ImportManager()
    imports.reserved_names |= {"copied", "pi", "os", "path"}
    imports.add_from("math", "pi")
    name = DeclarationValueSerializer.declare_function(module.copied, imports)
    assert name != "copied"
    namespace = {"copied": lambda *args: -100}
    exec("\n".join([*imports.render(), *imports.function_declarations]), namespace)  # noqa: S102
    result = namespace[name]()
    if inspect.isawaitable(result):
        result = await result
    assert result == expected
