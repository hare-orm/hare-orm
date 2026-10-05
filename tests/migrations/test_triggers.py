from __future__ import annotations

import pytest

from hare.ddl.enums import TriggerEvent, TriggerForEach, TriggerTiming
from hare.ddl.raw_sql_term import RawSQLTerm
from hare.ddl.schema_objects.trigger import Trigger
from hare.exceptions import ConfigurationError
from hare.query.expressions import Q


def test_trigger_defaults() -> None:
    trigger = Trigger(name="t", on=TriggerEvent.INSERT, body=RawSQLTerm("RETURN NEW;"))
    assert trigger.timing == TriggerTiming.AFTER
    assert trigger.for_each == TriggerForEach.ROW
    assert trigger.when is None
    assert trigger.language is None
    assert trigger.function_name == "t_fn"


@pytest.mark.parametrize("timing", ["before", "AFTERWARD", ""])
def test_trigger_rejects_invalid_timing(timing: str) -> None:
    with pytest.raises(ConfigurationError):
        Trigger(name="t", on=TriggerEvent.INSERT, body=RawSQLTerm("RETURN NEW;"), timing=timing)  # type: ignore[arg-type]


@pytest.mark.parametrize("for_each", ["row", "STATEMENTS", ""])
def test_trigger_rejects_invalid_for_each(for_each: str) -> None:
    with pytest.raises(ConfigurationError):
        Trigger(name="t", on=TriggerEvent.INSERT, body=RawSQLTerm("RETURN NEW;"), for_each=for_each)  # type: ignore[arg-type]


def test_trigger_deconstruct_omits_defaults() -> None:
    trigger = Trigger(name="t", on=TriggerEvent.INSERT, body=RawSQLTerm("RETURN NEW;"))
    path, args, kwargs = trigger.deconstruct()
    assert path == "hare.ddl.schema_objects.trigger.Trigger"
    assert args == []
    assert kwargs == {"name": "t", "on": "INSERT", "body": RawSQLTerm("RETURN NEW;")}


def test_trigger_deconstruct_includes_non_defaults() -> None:
    trigger = Trigger(
        name="t",
        on=TriggerEvent.UPDATE,
        body=RawSQLTerm("RETURN NEW;"),
        timing=TriggerTiming.BEFORE,
        for_each=TriggerForEach.STATEMENT,
        when=RawSQLTerm("OLD.x IS DISTINCT FROM NEW.x"),
        language="sql",
    )
    path, args, kwargs = trigger.deconstruct()
    assert kwargs == {
        "name": "t",
        "on": "UPDATE",
        "body": RawSQLTerm("RETURN NEW;"),
        "timing": TriggerTiming.BEFORE,
        "for_each": TriggerForEach.STATEMENT,
        "when": RawSQLTerm("OLD.x IS DISTINCT FROM NEW.x"),
        "language": "sql",
    }


def test_trigger_on_accepts_combined_events_as_raw_text() -> None:
    """TriggerEvent only covers the single-event case - a combination has no member of its own."""
    trigger = Trigger(name="t", on="INSERT OR UPDATE", body=RawSQLTerm("RETURN NEW;"))
    assert trigger.on == "INSERT OR UPDATE"


def test_trigger_equality_is_field_based() -> None:
    """StateModelDiff._generate_trigger_operations() relies on this to detect an unchanged
    trigger (same name, same everything else) vs. one that needs AlterTrigger."""
    a = Trigger(name="t", on=TriggerEvent.INSERT, body=RawSQLTerm("RETURN NEW;"))
    b = Trigger(name="t", on=TriggerEvent.INSERT, body=RawSQLTerm("RETURN NEW;"))
    c = Trigger(name="t", on=TriggerEvent.UPDATE, body=RawSQLTerm("RETURN NEW;"))
    assert a == b
    assert a != c


def test_trigger_is_frozen() -> None:
    trigger = Trigger(name="t", on=TriggerEvent.INSERT, body=RawSQLTerm("RETURN NEW;"))
    with pytest.raises(AttributeError):
        trigger.name = "other"  # type: ignore[misc]


def test_trigger_defaults_to_not_deferrable() -> None:
    trigger = Trigger(name="t", on=TriggerEvent.INSERT, body=RawSQLTerm("RETURN NEW;"))
    assert trigger.deferrable is False
    assert trigger.initially_deferred is False


def test_trigger_deferrable_constraint_trigger() -> None:
    trigger = Trigger(
        name="t",
        on=TriggerEvent.INSERT,
        body=RawSQLTerm("RETURN NEW;"),
        deferrable=True,
        initially_deferred=True,
    )
    assert trigger.deferrable is True
    assert trigger.initially_deferred is True


def test_trigger_initially_deferred_requires_deferrable() -> None:
    with pytest.raises(ConfigurationError):
        Trigger(name="t", on=TriggerEvent.INSERT, body=RawSQLTerm("RETURN NEW;"), initially_deferred=True)


@pytest.mark.parametrize("timing", [TriggerTiming.BEFORE, TriggerTiming.INSTEAD_OF])
def test_trigger_deferrable_rejects_non_after_timing(timing: TriggerTiming) -> None:
    """A Postgres CONSTRAINT TRIGGER can only ever be AFTER."""
    with pytest.raises(ConfigurationError):
        Trigger(name="t", on=TriggerEvent.INSERT, body=RawSQLTerm("RETURN NEW;"), timing=timing, deferrable=True)


def test_trigger_deferrable_rejects_statement_level() -> None:
    """A Postgres CONSTRAINT TRIGGER can only ever be FOR EACH ROW."""
    with pytest.raises(ConfigurationError):
        Trigger(
            name="t",
            on=TriggerEvent.INSERT,
            body=RawSQLTerm("RETURN NEW;"),
            for_each=TriggerForEach.STATEMENT,
            deferrable=True,
        )


def test_trigger_deconstruct_includes_deferrable_and_initially_deferred() -> None:
    trigger = Trigger(
        name="t",
        on=TriggerEvent.INSERT,
        body=RawSQLTerm("RETURN NEW;"),
        deferrable=True,
        initially_deferred=True,
    )
    path, args, kwargs = trigger.deconstruct()
    assert kwargs == {
        "name": "t",
        "on": "INSERT",
        "body": RawSQLTerm("RETURN NEW;"),
        "deferrable": True,
        "initially_deferred": True,
    }


def test_trigger_deconstruct_omits_deferrable_when_false() -> None:
    trigger = Trigger(name="t", on=TriggerEvent.INSERT, body=RawSQLTerm("RETURN NEW;"))
    _, _, kwargs = trigger.deconstruct()
    assert "deferrable" not in kwargs
    assert "initially_deferred" not in kwargs


def test_trigger_rejects_raw_sql_text() -> None:
    with pytest.raises(ConfigurationError, match="Trigger.body takes RawSQLTerm"):
        Trigger(name="t", on=TriggerEvent.INSERT, body="RETURN NEW;")  # type: ignore[arg-type]
    with pytest.raises(ConfigurationError, match="Trigger.when takes a Q"):
        Trigger(name="t", on=TriggerEvent.INSERT, body=RawSQLTerm("RETURN NEW;"), when="NEW.x > 1")  # type: ignore[arg-type]


def test_a_q_condition_reads_the_row_the_trigger_fires_for() -> None:
    def make(on: str) -> Trigger:
        return Trigger(name="t", on=on, body=RawSQLTerm("RETURN NEW;"), when=Q(x__gt=1))

    assert make("INSERT").get_condition_row() == "NEW"
    assert make("INSERT OR UPDATE OF x").get_condition_row() == "NEW"
    assert make("DELETE").get_condition_row() == "OLD"
    with pytest.raises(ConfigurationError, match="a DELETE reads the old row"):
        make("INSERT OR DELETE")


def test_a_q_condition_needs_a_row_trigger() -> None:
    with pytest.raises(ConfigurationError, match="STATEMENT trigger"):
        Trigger(
            name="t",
            on=TriggerEvent.INSERT,
            body=RawSQLTerm("RETURN NEW;"),
            for_each=TriggerForEach.STATEMENT,
            when=Q(x__gt=1),
        )
