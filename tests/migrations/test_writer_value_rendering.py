from __future__ import annotations

import datetime
import math
from enum import IntFlag

import pytest

from hare.exceptions import ConfigurationError
from hare.migrations.writer import ImportManager, MigrationWriter


class Permission(IntFlag):
    READ = 1
    WRITE = 2
    EXECUTE = 4


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
