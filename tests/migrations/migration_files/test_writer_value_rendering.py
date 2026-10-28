from __future__ import annotations

import collections.abc
import datetime
import math
import typing
from enum import IntFlag

import pytest

from hare.exceptions import ConfigurationError
from hare.migrations.writer import ImportManager, MigrationWriter

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
    source = MigrationWriter.render_value(value, imports)
    namespace: dict[str, object] = {}
    exec("\n".join(imports.render()), namespace)  # noqa: S102
    return eval(source, namespace)  # noqa: S307


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
        MigrationWriter.render_value([].append, ImportManager())


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
    source = MigrationWriter.render_call(path, [], {}, imports)
    namespace: dict[str, object] = {}
    exec("\n".join(imports.render()), namespace)  # noqa: S102
    called = eval(source.removesuffix("()"), namespace)  # noqa: S307
    assert source == expected_call
    assert f"{called.__module__}.{called.__qualname__}" == expected_class
    assert MigrationWriter.get_callable(path) is called


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
    assert render_and_evaluate(value) == value


@pytest.mark.parametrize(
    "value", [Pairs[str], globals()["Box[int]"]], ids=["type_alias_statement", "class_named_by_no_identifier"]
)
def test_a_type_a_migration_cant_import_is_refused(value: object) -> None:
    with pytest.raises(ConfigurationError):
        MigrationWriter.render_value(value, ImportManager())
