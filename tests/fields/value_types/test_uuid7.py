"""Uuid7 - version 7 UUIDs made in Python: the RFC 9562 layout, and increasing within a process even in one
millisecond or with the clock gone back; Python's own uuid.uuid7 where it has one."""

import uuid

import pytest

from hare.fields import constants as field_constants
from hare.fields.uuid7 import Uuid7

pytestmark = pytest.mark.database_independent


@pytest.fixture
def made_in_python(monkeypatch):
    """Uuid7 making its UUIDs itself, as on Python without uuid.uuid7."""
    monkeypatch.setattr("hare.fields.uuid7.UUID7_FACTORY", None)
    monkeypatch.setattr(Uuid7, "last_timestamp", 0)
    monkeypatch.setattr(Uuid7, "last_random", 0)


@pytest.mark.usefixtures("made_in_python")
def test_a_uuid_has_version_7_and_the_rfc_variant():
    value = Uuid7.new()
    assert value.version == 7
    assert value.variant == uuid.RFC_4122


@pytest.mark.usefixtures("made_in_python")
def test_uuids_made_one_after_another_increase():
    values = [Uuid7.new() for _ in range(10_000)]
    assert values == sorted(values)
    assert len(set(values)) == len(values)


@pytest.mark.usefixtures("made_in_python")
def test_a_clock_gone_back_still_gives_a_greater_uuid(monkeypatch):
    first = Uuid7.new()
    monkeypatch.setattr("hare.fields.uuid7.time.time_ns", lambda: 0)
    assert Uuid7.new() > first


@pytest.mark.usefixtures("made_in_python")
def test_a_full_counter_moves_to_the_next_millisecond(monkeypatch):
    monkeypatch.setattr("hare.fields.uuid7.time.time_ns", lambda: 5_000_000)
    monkeypatch.setattr(Uuid7, "last_timestamp", 5)
    monkeypatch.setattr(Uuid7, "last_random", field_constants.UUID7_RANDOM_MASK)
    value = Uuid7.new()
    assert value.int >> 80 == 6
    assert value.version == 7


def test_pythons_own_uuid7_is_used_where_there_is_one():
    if field_constants.UUID7_FACTORY is None:
        assert Uuid7.new().version == 7
    else:
        assert field_constants.UUID7_FACTORY is uuid.uuid7
