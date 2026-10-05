from __future__ import annotations

import pytest

from hare.ddl.constraints import UniqueConstraint
from hare.exceptions import ConfigurationError


def test_unique_constraint_defaults_to_not_deferrable() -> None:
    constraint = UniqueConstraint(fields=("email",))
    assert constraint.deferrable is False
    assert constraint.initially_deferred is False


def test_unique_constraint_deferrable_initially_deferred() -> None:
    constraint = UniqueConstraint(fields=("email",), deferrable=True, initially_deferred=True)
    assert constraint.deferrable is True
    assert constraint.initially_deferred is True


def test_unique_constraint_initially_deferred_requires_deferrable() -> None:
    with pytest.raises(ConfigurationError):
        UniqueConstraint(fields=("email",), initially_deferred=True)


def test_unique_constraint_deconstruct_includes_deferrable_and_initially_deferred() -> None:
    constraint = UniqueConstraint(fields=("email",), name="uq_email", deferrable=True, initially_deferred=True)
    path, args, kwargs = constraint.deconstruct()
    assert path == "hare.ddl.constraints.UniqueConstraint"
    assert args == []
    assert kwargs == {
        "fields": ["email"],
        "name": "uq_email",
        "deferrable": True,
        "initially_deferred": True,
    }


def test_unique_constraint_deconstruct_omits_deferrable_when_false() -> None:
    constraint = UniqueConstraint(fields=("email",), name="uq_email")
    _, _, kwargs = constraint.deconstruct()
    assert "deferrable" not in kwargs
    assert "initially_deferred" not in kwargs
