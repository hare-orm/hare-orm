from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from hare.migrations import Migration, State
from hare.migrations.execution.prepared_operation_execution import PreparedOperationExecution
from hare.migrations.operations import RunPython, SeparateDatabaseAndState
from hare.migrations.operations.hare_operation import HareOperation

pytestmark = pytest.mark.database_independent


class MarkSchema(HareOperation):
    def __init__(self, name, *, tenant_schema=False, atomic=False):
        self.name = name
        self.tenant_schema = tenant_schema
        self.atomic = atomic

    def state_forward(self, app_label, state):
        state.schemas.add(self.name)

    async def database_forward(self, app_label, old_state, new_state, state_editor):
        state_editor.events.append((self.name, set(old_state.schemas), set(new_state.schemas)))

    async def database_backward(self, app_label, old_state, new_state, state_editor):
        state_editor.events.append((self.name, set(old_state.schemas), set(new_state.schemas)))


@pytest.fixture
def editor():
    events = []

    async def check_constraints():
        events.append("constraints")

    return SimpleNamespace(
        client=SimpleNamespace(
            tenant_schema_template=None,
            tenant_schema=None,
            connection_alias="default",
            features=SimpleNamespace(supports_transactions=True),
        ),
        atomic_migration=False,
        collect_sql=False,
        collected_sql=[],
        events=events,
        check_constraints=check_constraints,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("backward", [False, True])
async def test_nested_database_operations_receive_actual_boundaries(editor, backward):
    migration = Migration(
        "nested",
        "app",
        operations=[
            SeparateDatabaseAndState(
                state_operations=[MarkSchema("declared")],
                database_operations=[
                    MarkSchema("first"),
                    SeparateDatabaseAndState(
                        database_operations=[MarkSchema("second")],
                    ),
                ],
            )
        ],
    )
    source = State()
    prepared = migration.prepare(source)
    await PreparedOperationExecution.run(migration, prepared, editor, backward=backward)
    changes = [event for event in editor.events if event != "constraints"]
    assert changes == (
        [("second", {"first", "second"}, {"first"}), ("first", {"first"}, set())]
        if backward
        else [("first", set(), {"first"}), ("second", {"first"}, {"first", "second"})]
    )
    assert prepared.after.schemas == {"declared"}
    assert source.schemas == set()


@pytest.mark.asyncio
@pytest.mark.parametrize("tenant", [None, "customer"])
async def test_separated_operations_route_each_leaf_to_its_schema(editor, tenant):
    editor.client.tenant_schema_template = "tenant_{name}"
    editor.client.tenant_schema = tenant
    migration = Migration(
        "mixed",
        "app",
        operations=[
            SeparateDatabaseAndState(
                database_operations=[
                    MarkSchema("shared"),
                    SeparateDatabaseAndState(
                        database_operations=[
                            MarkSchema("tenant", tenant_schema=True),
                        ]
                    ),
                ]
            )
        ],
    )
    await PreparedOperationExecution.run(migration, migration.prepare(State()), editor)
    assert [event[0] for event in editor.events if event != "constraints"] == [
        "shared" if tenant is None else "tenant",
    ]


@pytest.mark.asyncio
async def test_nested_atomic_operation_uses_one_transaction_and_restores_editor(editor, monkeypatch):
    original_client = editor.client
    boundaries = []

    @asynccontextmanager
    async def atomic():
        boundaries.append("begin")
        yield original_client
        boundaries.append("commit")

    monkeypatch.setattr(original_client, "_in_transaction", atomic, raising=False)
    inner = SeparateDatabaseAndState(database_operations=[MarkSchema("first", atomic=True)])
    inner.atomic = True
    migration = Migration("atomic", "app", operations=[inner])
    await PreparedOperationExecution.run(migration, migration.prepare(State()), editor)
    assert boundaries == ["begin", "commit"]
    assert editor.events.count("constraints") == 1
    assert editor.client is original_client
    assert editor.atomic_migration is False


@pytest.mark.asyncio
async def test_sql_collection_keeps_sql_siblings_beside_python_in_nested_branch(editor):
    calls = []
    editor.collect_sql = True
    migration = Migration(
        "collect",
        "app",
        operations=[
            SeparateDatabaseAndState(
                database_operations=[
                    RunPython(lambda apps, schema_editor: calls.append("python")),
                    MarkSchema("sql"),
                ]
            )
        ],
    )
    await PreparedOperationExecution.run(migration, migration.prepare(State()), editor, collect_sql=True)
    assert calls == []
    assert "-- THIS OPERATION CANNOT BE REPRESENTED AS SQL" in editor.collected_sql
    assert editor.events == [("sql", set(), {"sql"})]
