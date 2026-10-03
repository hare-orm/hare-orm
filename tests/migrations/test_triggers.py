from __future__ import annotations

import pytest

from hare.ddl.enums import TriggerEvent, TriggerForEach, TriggerTiming
from hare.ddl.triggers import Trigger
from hare.exceptions import ConfigurationError


def test_trigger_defaults() -> None:
    trigger = Trigger(name="t", on=TriggerEvent.INSERT, body="RETURN NEW;")
    assert trigger.timing == TriggerTiming.AFTER
    assert trigger.for_each == TriggerForEach.ROW
    assert trigger.when is None
    assert trigger.language is None
    assert trigger.function_name == "t_fn"


@pytest.mark.parametrize("timing", ["before", "AFTERWARD", ""])
def test_trigger_rejects_invalid_timing(timing: str) -> None:
    with pytest.raises(ConfigurationError):
        Trigger(name="t", on=TriggerEvent.INSERT, body="RETURN NEW;", timing=timing)  # type: ignore[arg-type]


@pytest.mark.parametrize("for_each", ["row", "STATEMENTS", ""])
def test_trigger_rejects_invalid_for_each(for_each: str) -> None:
    with pytest.raises(ConfigurationError):
        Trigger(name="t", on=TriggerEvent.INSERT, body="RETURN NEW;", for_each=for_each)  # type: ignore[arg-type]


def test_trigger_deconstruct_omits_defaults() -> None:
    trigger = Trigger(name="t", on=TriggerEvent.INSERT, body="RETURN NEW;")
    path, args, kwargs = trigger.deconstruct()
    assert path == "hare.ddl.triggers.Trigger"
    assert args == []
    assert kwargs == {"name": "t", "on": "INSERT", "body": "RETURN NEW;"}


def test_trigger_deconstruct_includes_non_defaults() -> None:
    trigger = Trigger(
        name="t",
        on=TriggerEvent.UPDATE,
        body="RETURN NEW;",
        timing=TriggerTiming.BEFORE,
        for_each=TriggerForEach.STATEMENT,
        when="OLD.x IS DISTINCT FROM NEW.x",
        language="sql",
    )
    path, args, kwargs = trigger.deconstruct()
    assert kwargs == {
        "name": "t",
        "on": "UPDATE",
        "body": "RETURN NEW;",
        "timing": TriggerTiming.BEFORE,
        "for_each": TriggerForEach.STATEMENT,
        "when": "OLD.x IS DISTINCT FROM NEW.x",
        "language": "sql",
    }


def test_trigger_on_accepts_combined_events_as_raw_text() -> None:
    """TriggerEvent only covers the single-event case - a combination has no member of its own."""
    trigger = Trigger(name="t", on="INSERT OR UPDATE", body="RETURN NEW;")
    assert trigger.on == "INSERT OR UPDATE"


def test_trigger_equality_is_field_based() -> None:
    """StateModelDiff._generate_trigger_operations() relies on this to detect an unchanged
    trigger (same name, same everything else) vs. one that needs AlterTrigger."""
    a = Trigger(name="t", on=TriggerEvent.INSERT, body="RETURN NEW;")
    b = Trigger(name="t", on=TriggerEvent.INSERT, body="RETURN NEW;")
    c = Trigger(name="t", on=TriggerEvent.UPDATE, body="RETURN NEW;")
    assert a == b
    assert a != c


def test_trigger_is_frozen() -> None:
    trigger = Trigger(name="t", on=TriggerEvent.INSERT, body="RETURN NEW;")
    with pytest.raises(AttributeError):
        trigger.name = "other"  # type: ignore[misc]


def test_trigger_defaults_to_not_deferrable() -> None:
    trigger = Trigger(name="t", on=TriggerEvent.INSERT, body="RETURN NEW;")
    assert trigger.deferrable is False
    assert trigger.initially_deferred is False


def test_trigger_deferrable_constraint_trigger() -> None:
    trigger = Trigger(
        name="t",
        on=TriggerEvent.INSERT,
        body="RETURN NEW;",
        deferrable=True,
        initially_deferred=True,
    )
    assert trigger.deferrable is True
    assert trigger.initially_deferred is True


def test_trigger_initially_deferred_requires_deferrable() -> None:
    with pytest.raises(ConfigurationError):
        Trigger(name="t", on=TriggerEvent.INSERT, body="RETURN NEW;", initially_deferred=True)


@pytest.mark.parametrize("timing", [TriggerTiming.BEFORE, TriggerTiming.INSTEAD_OF])
def test_trigger_deferrable_rejects_non_after_timing(timing: TriggerTiming) -> None:
    """A Postgres CONSTRAINT TRIGGER can only ever be AFTER."""
    with pytest.raises(ConfigurationError):
        Trigger(name="t", on=TriggerEvent.INSERT, body="RETURN NEW;", timing=timing, deferrable=True)


def test_trigger_deferrable_rejects_statement_level() -> None:
    """A Postgres CONSTRAINT TRIGGER can only ever be FOR EACH ROW."""
    with pytest.raises(ConfigurationError):
        Trigger(
            name="t",
            on=TriggerEvent.INSERT,
            body="RETURN NEW;",
            for_each=TriggerForEach.STATEMENT,
            deferrable=True,
        )


def test_trigger_deconstruct_includes_deferrable_and_initially_deferred() -> None:
    trigger = Trigger(
        name="t",
        on=TriggerEvent.INSERT,
        body="RETURN NEW;",
        deferrable=True,
        initially_deferred=True,
    )
    path, args, kwargs = trigger.deconstruct()
    assert kwargs == {
        "name": "t",
        "on": "INSERT",
        "body": "RETURN NEW;",
        "deferrable": True,
        "initially_deferred": True,
    }


def test_trigger_deconstruct_omits_deferrable_when_false() -> None:
    trigger = Trigger(name="t", on=TriggerEvent.INSERT, body="RETURN NEW;")
    _, _, kwargs = trigger.deconstruct()
    assert "deferrable" not in kwargs
    assert "initially_deferred" not in kwargs
