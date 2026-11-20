from __future__ import annotations

import asyncio
import contextvars
from types import SimpleNamespace

import pytest

from hare.core.hare_context import HareContext
from hare.core.routing.primary_reads import PrimaryReads
from hare.core.routing.routing import Routing
from hare.core.routing.written_connections import WrittenConnections
from hare.dialects.base.client.transaction_lifecycle.transaction_ending import TransactionEnding
from hare.models.write.write_steps import WriteSteps
from hare.time import Timezone

pytestmark = pytest.mark.database_independent


def test_same_connection_alias_does_not_share_write_history_between_applications():
    first, second = HareContext(), HareContext()
    first._router = second._router = SimpleNamespace(has_routers=True)
    token = WrittenConnections.current.set(None)
    try:
        with first:
            WrittenConnections.record(
                SimpleNamespace(connection_handler=first.connections, connection_alias="default")
            )
            first_state = WrittenConnections.current.get()
            assert first_state.reads_from("default", None)
            with second:
                assert not first_state.reads_from("default", None)
                WrittenConnections.record(
                    SimpleNamespace(connection_handler=second.connections, connection_alias="default")
                )
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
            WrittenConnections.record(
                SimpleNamespace(connection_handler=first.connections, connection_alias="default")
            )
            with second:
                WrittenConnections.record(
                    SimpleNamespace(connection_handler=second.connections, connection_alias="default")
                )
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
            WrittenConnections.record(
                SimpleNamespace(connection_handler=application.connections, connection_alias="first")
            )
            parent = WrittenConnections.current.get()

            async def write():
                assert WrittenConnections.current.get() is parent
                WrittenConnections.record(
                    SimpleNamespace(connection_handler=application.connections, connection_alias="second")
                )
                assert WrittenConnections.current.get().reads_from("first", None)
                assert WrittenConnections.current.get().reads_from("second", None)

            await asyncio.create_task(write())
            assert WrittenConnections.current.get() is parent
            assert not parent.reads_from("second", None)
            with pytest.raises(TypeError):
                parent.written_at_by_connection["first"] = 0
    finally:
        WrittenConnections.current.reset(token)


@pytest.mark.parametrize("ambient", [False, True])
def test_explicit_client_records_write_history_for_its_application(ambient):
    first, second = HareContext(), HareContext()
    first._router = SimpleNamespace(has_routers=True)
    client = SimpleNamespace(connection_handler=first.connections, connection_alias="default")
    token = WrittenConnections.current.set(None)
    try:
        if ambient:
            with second:
                WrittenConnections.record(client)
                assert not WrittenConnections.current.get().reads_from("default", None)
        else:
            WrittenConnections.record(client)
        with first:
            assert WrittenConnections.current.get().reads_from("default", None)
        with second:
            assert not WrittenConnections.current.get().reads_from("default", None)
    finally:
        WrittenConnections.current.reset(token)


@pytest.mark.asyncio
async def test_reconnecting_keeps_the_applications_write_history():
    application = HareContext()
    application._router = SimpleNamespace(has_routers=True)
    token = WrittenConnections.current.set(None)
    try:
        with application:
            handler = application.connections
            previous_epoch = handler.application
            WrittenConnections.record(SimpleNamespace(connection_handler=handler, connection_alias="default"))
            await handler.close_all(discard=False)
            assert handler.application is not previous_epoch
            assert WrittenConnections.current.get().reads_from("default", None)
    finally:
        WrittenConnections.current.reset(token)


@pytest.mark.asyncio
@pytest.mark.parametrize("entrypoint", ["whole_write", "transaction_end"])
@pytest.mark.parametrize("fails", [False, True])
async def test_internal_operations_publish_writes_without_merging_user_task_context(monkeypatch, entrypoint, fails):
    application = HareContext()
    application._router = SimpleNamespace(has_routers=True)
    monkeypatch.setattr(WrittenConnections, "is_recording", True)
    marker = contextvars.ContextVar("operation_marker", default="parent")
    token = WrittenConnections.current.set(None)
    client = SimpleNamespace(
        connection_handler=application.connections,
        connection_alias="operation",
        features=SimpleNamespace(supports_transactions=False),
    )
    child_client = SimpleNamespace(connection_handler=application.connections, connection_alias="user_child")

    async def user_child():
        WrittenConnections.record(child_client)

    async def operation():
        WrittenConnections.record(client)
        marker.set("operation")
        await asyncio.create_task(user_child())
        if fails:
            raise ValueError("operation failed after writing")
        return 7

    try:
        with application:
            if entrypoint == "whole_write":
                run = WriteSteps.run_whole(client, operation())
            else:
                run = TransactionEnding.run_shielded_from_cancellation(operation())
            if fails:
                with pytest.raises(ValueError, match="operation failed after writing"):
                    await run
            else:
                assert await run == 7
            history = WrittenConnections.current.get()
            assert history.reads_from("operation", None)
            assert not history.reads_from("user_child", None)
            assert marker.get() == "parent"
    finally:
        WrittenConnections.current.reset(token)


def test_internal_write_publication_does_not_restore_inherited_forgotten_entries():
    application = HareContext()
    application._router = SimpleNamespace(has_routers=True)
    token = WrittenConnections.current.set(None)
    try:
        with application:
            client = SimpleNamespace(connection_handler=application.connections, connection_alias="inherited")
            WrittenConnections.record(client)
            previous = WrittenConnections.current.get()
            operation_context = contextvars.copy_context()
            operation_context.run(
                WrittenConnections.record,
                SimpleNamespace(connection_handler=application.connections, connection_alias="operation"),
            )
            Routing.forget_writes()
            WrittenConnections.record(
                SimpleNamespace(connection_handler=application.connections, connection_alias="caller")
            )
            WrittenConnections.publish_from_context(operation_context, previous)
            history = WrittenConnections.current.get()
            assert history.reads_from("operation", None)
            assert history.reads_from("caller", None)
            assert not history.reads_from("inherited", None)
            assert previous.reads_from("inherited", None)
            assert not previous.reads_from("operation", None)
    finally:
        WrittenConnections.current.reset(token)
