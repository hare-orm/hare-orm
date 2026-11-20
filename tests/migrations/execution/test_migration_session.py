from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from hare.core.hare_context import HareContext
from hare.fields import IntField
from hare.migrations.api import MigrationSession, migrate, plan
from hare.migrations.exceptions import UnknownMigrationError
from hare.migrations.execution.executor.migration_executor import MigrationExecutor
from hare.migrations.loading.graph.migration_key import MigrationKey
from hare.migrations.loading.migration_definitions import MigrationDefinitions
from hare.migrations.loading.migration_loader import MigrationLoader
from hare.migrations.loading.recorder.noop_recorder import NoopRecorder
from hare.migrations.migration import Migration
from hare.migrations.operations import RunPython, RunSQL, SeparateDatabaseAndState


@pytest.mark.database_independent
@pytest.mark.parametrize("nested", [False, True])
@pytest.mark.parametrize("bound_method", [False, True])
def test_definition_snapshots_preserve_callback_resources(nested, bound_method):
    class Callback:
        def __deepcopy__(self, memo):
            raise AssertionError("A callback's external resources must not be copied")

        def __call__(self, *args):
            raise AssertionError("Snapshotting must not run a data callback")

    callback = Callback()
    code = callback.__call__ if bound_method else callback
    operation = RunPython(code)
    if nested:
        operation = SeparateDatabaseAndState(database_operations=[operation])
    key = MigrationKey("library", "0001_initial")
    source = MigrationLoader({}, NoopRecorder())
    source.disk_migrations[key] = Migration(key.name, key.app_label, operations=[operation])
    definitions = MigrationDefinitions.capture(source)
    copied = definitions.copy_migrations()[key].operations[0]
    assert copied is not operation
    if nested:
        assert copied.database_operations is not operation.database_operations
        copied = copied.database_operations[0]
    assert copied.code is code


@pytest.mark.database_independent
def test_definition_snapshots_use_custom_operation_protocol():
    class CustomOperation(RunSQL):
        def snapshot(self):
            return type(self)(list(self.sql))

        def __deepcopy__(self, memo):
            raise AssertionError("The operation snapshot protocol must own declaration copying")

    key = MigrationKey("library", "0001_initial")
    operation = CustomOperation(["SELECT 1"])
    source = MigrationLoader({}, NoopRecorder())
    source.disk_migrations[key] = Migration(key.name, key.app_label, operations=[operation])
    definitions = MigrationDefinitions.capture(source)
    copied = definitions.copy_migrations()[key].operations[0]
    copied.sql.append("SELECT 2")
    assert definitions.migrations[key].operations[0].sql == ["SELECT 1"]
    assert operation.sql == ["SELECT 1"]


@pytest.mark.database_independent
@pytest.mark.asyncio
async def test_definitions_are_detached_while_each_graph_refresh_reads_the_journal():
    key = MigrationKey("library", "0001_initial")
    source = MigrationLoader({}, NoopRecorder())
    source.disk_migrations[key] = Migration(key.name, key.app_label, operations=[RunSQL("SELECT 1")])
    source.migrated_apps.add("library")
    definitions = MigrationDefinitions.capture(source)
    recorder = NoopRecorder()
    recorder.applied_migrations = AsyncMock(side_effect=[[], [key]])
    loader = MigrationLoader({}, recorder, definitions=definitions)
    source.disk_migrations[key].operations.clear()
    await loader.build_graph()
    assert loader.disk_migrations[key].operations
    assert loader.applied_migrations == set()
    loader.disk_migrations[key].operations.clear()
    await loader.build_graph()
    assert loader.disk_migrations[key].operations
    assert loader.applied_migrations == {key}
    assert recorder.applied_migrations.await_count == 2


@pytest.mark.database_independent
@pytest.mark.asyncio
async def test_api_preserves_callers_context_and_prints_nothing(capsys):
    config = {"connections": {"default": "sqlite+aiosqlite://:memory:"}, "apps": {}}
    with HareContext() as caller:
        assert await plan(config=config) == []
        assert await migrate(config=config) == []
        assert HareContext.require_current() is caller
        with pytest.raises(UnknownMigrationError):
            await plan(config=config, app_labels=["missing"])
        assert HareContext.require_current() is caller
    assert capsys.readouterr().out == ""


@pytest.mark.database_independent
@pytest.mark.asyncio
async def test_session_preserves_body_and_cleanup_failures(monkeypatch):
    config = {"connections": {"default": "sqlite+aiosqlite://:memory:"}, "apps": {}}
    session = MigrationSession(config)
    body_error, close_error = ValueError("body failed"), RuntimeError("close failed")
    with HareContext() as caller:
        with pytest.raises(ExceptionGroup) as captured:
            async with session:
                monkeypatch.setattr(session.context, "close_connections", AsyncMock(side_effect=close_error))
                raise body_error
        assert HareContext.require_current() is caller
    assert captured.value.exceptions == (body_error, close_error)


@pytest.mark.sqlite_only
@pytest.mark.asyncio
async def test_one_session_keeps_definitions_when_files_change(tmp_path, monkeypatch):
    package = tmp_path / "definition_snapshot_app"
    migrations = package / "migrations"
    migrations.mkdir(parents=True)
    for path in (package / "__init__.py", package / "models.py", migrations / "__init__.py"):
        path.write_text("", encoding="utf-8")
    path = migrations / "0001_initial.py"
    source = (
        "from hare.migrations import Migration as BaseMigration, operations\n"
        "class Migration(BaseMigration):\n"
        "    operations = [operations.RunSQL('SELECT 1')]\n"
    )
    path.write_text(source, encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    config = {
        "connections": {"default": "sqlite+aiosqlite://:memory:"},
        "apps": {
            "library": {
                "models": ["definition_snapshot_app.models"],
                "migrations": "definition_snapshot_app.migrations",
            }
        },
    }
    async with MigrationSession(config) as session:
        initial = session.get_definitions()
        path.write_text(source.replace("SELECT 1", "SELECT 2"), encoding="utf-8")
        assert session.get_definitions() is initial
        assert next(iter(initial.migrations.values())).operations[0].sql == "SELECT 1"
    async with MigrationSession(config) as session:
        assert next(iter(session.get_definitions().migrations.values())).operations[0].sql == "SELECT 2"


@pytest.mark.database_independent
@pytest.mark.parametrize("bound_method", [False, True])
@pytest.mark.parametrize("nested", [False, True])
def test_operation_snapshot_includes_inherited_slots(bound_method, nested):
    class Callback:
        def __deepcopy__(self, memo):
            raise AssertionError("The callback's resources must retain their identity")

        def __call__(self, *args):
            raise AssertionError("A declaration snapshot must not invoke callbacks")

    class DeclaredField(IntField):
        def snapshot(self, *, memo=None):
            result = super().snapshot(memo=memo)
            result.description = "snapshotted field"
            return result

    class ParentOperation(RunSQL):
        __slots__ = ("declaration",)

    class SlottedOperation(ParentOperation):
        __slots__ = ("callback", "values")

    callback = Callback()
    code = callback.__call__ if bound_method else callback
    field = DeclaredField(default=code)
    operation = SlottedOperation(["SELECT 1"])
    operation.declaration = field
    operation.callback = code
    operation.values = [field, {"callback": code, "mutable": []}]
    operation.extra = operation.values[1]["mutable"]
    operation.values.append(operation.values)
    source = SeparateDatabaseAndState(state_operations=[operation]) if nested else operation
    copied = source.snapshot()
    if nested:
        assert copied.state_operations is not source.state_operations
        copied = copied.state_operations[0]
    assert copied is not operation
    assert copied.callback is code
    assert copied.declaration is not field
    assert copied.declaration.description == "snapshotted field"
    assert copied.declaration.default is code
    assert copied.values[0] is copied.declaration
    assert copied.values[1]["callback"] is code
    assert copied.values[-1] is copied.values
    assert copied.extra is copied.values[1]["mutable"]
    copied.values[1]["mutable"].append("changed")
    copied.sql.append("SELECT 2")
    assert operation.values[1]["mutable"] == []
    assert operation.sql == ["SELECT 1"]
    assert field.description != "snapshotted field"


@pytest.mark.database_independent
@pytest.mark.asyncio
@pytest.mark.parametrize("callback_type", ["sync", "async", "future"])
async def test_migration_progress_waits_for_callback_completion(callback_type):
    entered, release = asyncio.Event(), asyncio.Event()
    future = asyncio.get_running_loop().create_future()
    calls = []

    async def async_callback(*args):
        calls.append(args)
        entered.set()
        await release.wait()

    def callback(*args):
        if callback_type == "async":
            return async_callback(*args)
        calls.append(args)
        entered.set()
        return future if callback_type == "future" else 42

    key = MigrationKey("library", "0001_initial")
    task = asyncio.create_task(MigrationExecutor._emit(callback, "apply_start", key))
    try:
        await entered.wait()
        if callback_type != "sync":
            assert not task.done()
    finally:
        release.set()
        future.set_result(None)
    await task
    assert calls == [("apply_start", "library", "0001_initial")]


@pytest.mark.database_independent
@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [RuntimeError("progress"), asyncio.CancelledError("progress")])
async def test_migration_progress_propagates_callback_failure(failure):
    async def callback(*args):
        raise failure

    with pytest.raises(type(failure)) as captured:
        await MigrationExecutor._emit(callback, "rollback_done", MigrationKey("library", "0001_initial"))
    assert captured.value is failure
