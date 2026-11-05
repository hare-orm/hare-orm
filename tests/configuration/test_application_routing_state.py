from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from hare.core.hare_context import HareContext
from hare.core.routing.primary_reads import PrimaryReads
from hare.core.routing.routing import Routing
from hare.core.routing.written_connections import WrittenConnections
from hare.time import Timezone

pytestmark = pytest.mark.database_independent


def test_same_connection_alias_does_not_share_write_history_between_applications():
    first, second = HareContext(), HareContext()
    first._router = second._router = SimpleNamespace(has_routers=True)
    token = WrittenConnections.current.set(None)
    try:
        with first:
            WrittenConnections.record("default")
            first_state = WrittenConnections.current.get()
            assert first_state.reads_from("default", None)
            with second:
                assert not first_state.reads_from("default", None)
                WrittenConnections.record("default")
                assert WrittenConnections.current.get().reads_from("default", None)
            assert WrittenConnections.current.get().reads_from("default", None)
            assert first_state.reads_from("default", None)
    finally:
        WrittenConnections.current.reset(token)


def test_primary_read_scope_applies_to_its_application_only():
    first, second = HareContext(), HareContext()
    with first, PrimaryReads():
        assert PrimaryReads.is_active()
        with second:
            assert not PrimaryReads.is_active()
            with PrimaryReads():
                assert PrimaryReads.is_active()
        assert PrimaryReads.is_active()
    assert not PrimaryReads.is_active()


def test_forgetting_writes_preserves_other_applications_and_published_snapshots():
    first, second = HareContext(), HareContext()
    first._router = second._router = SimpleNamespace(has_routers=True)
    token = WrittenConnections.current.set(None)
    try:
        with first:
            WrittenConnections.record("default")
            with second:
                WrittenConnections.record("default")
                snapshot = WrittenConnections.current.get()
                Routing.forget_writes()
                assert not WrittenConnections.current.get().reads_from("default", None)
                assert snapshot.reads_from("default", None)
            assert WrittenConnections.current.get().reads_from("default", None)
            Routing.forget_writes()
            assert WrittenConnections.current.get() is None
            assert snapshot.reads_from("default", None)
    finally:
        WrittenConnections.current.reset(token)


def test_timezone_override_is_local_to_its_application():
    first, second = HareContext(), HareContext()
    with first, Timezone.override("Europe/Paris"):
        assert Timezone.name() == "Europe/Paris"
        with second:
            assert Timezone.name() == second._timezone
            with Timezone.override("Asia/Tokyo"):
                assert Timezone.name() == "Asia/Tokyo"
        assert Timezone.name() == "Europe/Paris"


@pytest.mark.asyncio
async def test_child_write_publishes_a_new_immutable_history():
    application = HareContext()
    application._router = SimpleNamespace(has_routers=True)
    token = WrittenConnections.current.set(None)
    try:
        with application:
            WrittenConnections.record("first")
            parent = WrittenConnections.current.get()

            async def write():
                assert WrittenConnections.current.get() is parent
                WrittenConnections.record("second")
                assert WrittenConnections.current.get().reads_from("first", None)
                assert WrittenConnections.current.get().reads_from("second", None)

            await asyncio.create_task(write())
            assert WrittenConnections.current.get() is parent
            assert not parent.reads_from("second", None)
            with pytest.raises(TypeError):
                parent.written_at_by_connection["first"] = 0
    finally:
        WrittenConnections.current.reset(token)
