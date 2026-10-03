from __future__ import annotations

from typing import Any, cast

import pytest

from hare import fields
from hare.ddl import RawSQLTerm
from hare.ddl.constraints import CheckConstraint, UniqueConstraint
from hare.ddl.indexes import Index
from hare.dialects.base.schema.editor import BaseSchemaEditor
from hare.dialects.postgresql.schema.editor import PostgresqlSchemaEditor
from hare.fields.base import Field
from hare.fields.relations.fields import ForeignKeyFieldInstance, ManyToManyFieldInstance
from hare.migrations.exceptions import IncompatibleStateError
from hare.migrations.operations import (
    AddConstraint,
    AddField,
    AddIndex,
    AlterField,
    CreateModel,
    DeleteModel,
    RemoveField,
    RemoveIndex,
    RenameConstraint,
    RenameIndex,
    RenameModel,
    RunPython,
    RunSQL,
)
from hare.migrations.state.apps import StateApps
from hare.migrations.state.project import ModelState, State
from hare.models import Model
from tests.utils.fake_client import FakeClient, MockIntrospectionClient


class TestSchemaEditor(BaseSchemaEditor):
    def _get_table_comment_sql(self, table: str, comment: str) -> str:
        return ""

    def _get_column_comment_sql(self, table: str, column: str, comment: str) -> str:
        return ""


def make_model(
    model_name: str,
    *,
    meta_options: dict[str, Any] | None = None,
    **model_fields: Field,
) -> type[Model]:
    attrs: dict[str, Any] = dict(model_fields)
    options: dict[str, Any] = {"app": "models", "table": "widget"}
    if meta_options:
        options.update(meta_options)
    attrs["Meta"] = type("Meta", (), options)
    return type(model_name, (Model,), attrs)


def build_state(app_label: str, model: type) -> State:
    apps = StateApps()
    state = State(models={}, apps=apps)
    model_state = ModelState.make_from_model(app_label, model)
    state.models[(app_label, model.__name__)] = model_state
    model_clone = model_state.render(apps)
    apps.register_model(app_label, model_clone)
    return state


@pytest.mark.asyncio
async def test_create_model_operation_runs_sql() -> None:
    client = FakeClient("sql")
    editor = TestSchemaEditor(client)
    state = State(models={}, apps=StateApps())

    op = CreateModel(
        name="Widget",
        fields=[("id", fields.IntField(primary_key=True)), ("name", fields.TextField())],
    )

    await op.run("models", state, dry_run=False, state_editor=editor)

    assert len(client.executed) == 1
    assert client.executed[0] == (
        'CREATE TABLE "widget" (\n    "id" INT NOT NULL PRIMARY KEY,\n    "name" TEXT NOT NULL\n);'
    )


@pytest.mark.asyncio
async def test_create_model_operation_emits_check_and_unique_constraint_ddl() -> None:
    """Meta.constraints used to be recorded only in the diffing state, and NEVER translated into
    DDL when creating a NEW model - only add_constraint() on an already-existing one via
    AddConstraint. Regression test for BaseSchemaEditor.create_model()."""
    client = FakeClient("sql")
    editor = TestSchemaEditor(client)
    state = State(models={}, apps=StateApps())

    op = CreateModel(
        name="Widget",
        fields=[("id", fields.IntField(primary_key=True)), ("price", fields.IntField())],
        options={
            "constraints": (
                CheckConstraint(check=RawSQLTerm("price > 0"), name="ck_widget_price_positive"),
                UniqueConstraint(fields=("price",), name="uq_widget_price"),
            )
        },
    )

    await op.run("models", state, dry_run=False, state_editor=editor)

    # Both are part of the CREATE TABLE itself.
    assert len(client.executed) == 1
    assert "CREATE TABLE" in client.executed[0]
    assert 'CONSTRAINT "ck_widget_price_positive" CHECK (price > 0)' in client.executed[0]
    assert 'CONSTRAINT "uq_widget_price" UNIQUE ("price")' in client.executed[0]


@pytest.mark.asyncio
async def test_create_model_operation_emits_unique_index_ddl() -> None:
    """CreateModel's own Meta.indexes rendering used to call _get_index_sql() without passing
    unique=index.unique (unlike AddIndex's own Index.get_sql(), which already forwarded it
    correctly) - an Index(unique=True) declared in Meta.indexes silently created a PLAIN index
    the moment the model went through CreateModel (any brand-new model, or an initial migration
    after a squash), with no error to signal the lost uniqueness."""
    client = FakeClient("sql")
    editor = TestSchemaEditor(client)
    state = State(models={}, apps=StateApps())

    op = CreateModel(
        name="Widget",
        fields=[("id", fields.IntField(primary_key=True)), ("price", fields.IntField())],
        options={"indexes": (Index(fields=("price",), unique=True, name="uq_idx_widget_price"),)},
    )

    await op.run("models", state, dry_run=False, state_editor=editor)

    assert len(client.executed) == 1
    assert 'CREATE UNIQUE INDEX "uq_idx_widget_price" ON "widget" ("price")' in client.executed[0]


@pytest.mark.asyncio
async def test_add_field_operation_runs_sql() -> None:
    client = FakeClient("sql")
    editor = TestSchemaEditor(client)
    state = State(models={}, apps=StateApps())
    CreateModel(name="Widget", fields=[("id", fields.IntField(primary_key=True))]).state_forward("models", state)

    op = AddField(model_name="Widget", name="name", field=fields.TextField())

    await op.run("models", state, dry_run=False, state_editor=editor)

    assert len(client.executed) == 1
    assert client.executed[0] == 'ALTER TABLE "widget" ADD COLUMN "name" TEXT NOT NULL'


@pytest.mark.asyncio
async def test_delete_model_operation_runs_sql() -> None:
    client = FakeClient("sql")
    editor = TestSchemaEditor(client)
    state = State(models={}, apps=StateApps())
    CreateModel(name="Widget", fields=[("id", fields.IntField(primary_key=True))]).state_forward("models", state)

    op = DeleteModel(name="Widget")

    await op.run("models", state, dry_run=False, state_editor=editor)

    assert len(client.executed) == 1
    assert client.executed[0] == 'DROP TABLE "widget" CASCADE'


@pytest.mark.asyncio
async def test_m2m_through_model_migration_lifecycle_emits_no_extra_through_table_ddl() -> None:
    """A ManyToManyField(through=Model) must not get hare's own bespoke through-table DDL at
    any point in the migration lifecycle - CreateModel, AddField (an extra column on the through
    model itself), and RemoveField (the M2M field, not the through model) must all leave the
    through model's own table alone, since it's managed by its OWN CreateModel/AddField/etc."""
    client = FakeClient("sql")
    editor = TestSchemaEditor(client)
    state = State(models={}, apps=StateApps())

    await CreateModel(
        name="Person",
        fields=[
            ("id", fields.IntField(primary_key=True)),
            (
                "groups",
                ManyToManyFieldInstance("models.Group", through="models.Membership", related_name="members"),
            ),
        ],
    ).run("models", state, dry_run=False, state_editor=editor)
    assert client.executed == ['CREATE TABLE "person" (\n    "id" INT NOT NULL PRIMARY KEY\n);']
    client.executed.clear()

    await CreateModel(name="Group", fields=[("id", fields.IntField(primary_key=True))]).run(
        "models", state, dry_run=False, state_editor=editor
    )
    client.executed.clear()

    await CreateModel(
        name="Membership",
        fields=[
            ("id", fields.IntField(primary_key=True)),
            ("person", ForeignKeyFieldInstance("models.Person", related_name="membership_rows")),
            ("group", ForeignKeyFieldInstance("models.Group", related_name="membership_rows")),
        ],
    ).run("models", state, dry_run=False, state_editor=editor)
    # Exactly one CREATE TABLE for "membership" itself - no separate opaque M2M through table.
    assert len(client.executed) == 1
    assert 'CREATE TABLE "membership"' in client.executed[0]
    assert '"person_id" INT NOT NULL REFERENCES "person"' in client.executed[0]
    assert '"group_id" INT NOT NULL REFERENCES "group"' in client.executed[0]
    client.executed.clear()

    person = state.apps.get_model("models.Person")
    m2m_field = cast(ManyToManyFieldInstance, person._meta.fields_map["groups"])
    assert m2m_field.through == "membership"
    assert m2m_field.forward_keys == ("group_id",)
    assert m2m_field.backward_keys == ("person_id",)

    # AddField on the through model itself (an ordinary extra column) - normal ADD COLUMN DDL.
    await AddField(model_name="Membership", name="note", field=fields.CharField(max_length=10, null=True)).run(
        "models", state, dry_run=False, state_editor=editor
    )
    assert client.executed == ['ALTER TABLE "membership" ADD COLUMN "note" VARCHAR(10)']
    client.executed.clear()

    # Removing the M2M field itself must NOT drop the through model's table - it's still a real,
    # independently-tracked model.
    await RemoveField(model_name="Person", name="groups").run("models", state, dry_run=False, state_editor=editor)
    assert client.executed == []
    client.executed.clear()

    # Deleting the through model itself IS a real DROP TABLE - via its own DeleteModel.
    await DeleteModel(name="Membership").run("models", state, dry_run=False, state_editor=editor)
    assert client.executed == ['DROP TABLE "membership" CASCADE']


@pytest.mark.asyncio
async def test_delete_model_with_m2m_through_model_does_not_redundantly_drop_through_table() -> None:
    """Deleting the M2M field's owning model, AFTER its through model was already dropped by its
    own separate DeleteModel, must not try to drop that table a second time."""
    client = FakeClient("sql")
    editor = TestSchemaEditor(client)
    state = State(models={}, apps=StateApps())

    await CreateModel(
        name="Person",
        fields=[
            ("id", fields.IntField(primary_key=True)),
            (
                "groups",
                ManyToManyFieldInstance("models.Group", through="models.Membership", related_name="members"),
            ),
        ],
    ).run("models", state, dry_run=False, state_editor=editor)
    await CreateModel(name="Group", fields=[("id", fields.IntField(primary_key=True))]).run(
        "models", state, dry_run=False, state_editor=editor
    )
    await CreateModel(
        name="Membership",
        fields=[
            ("id", fields.IntField(primary_key=True)),
            ("person", ForeignKeyFieldInstance("models.Person", related_name="membership_rows")),
            ("group", ForeignKeyFieldInstance("models.Group", related_name="membership_rows")),
        ],
    ).run("models", state, dry_run=False, state_editor=editor)
    client.executed.clear()

    await DeleteModel(name="Membership").run("models", state, dry_run=False, state_editor=editor)
    assert client.executed == ['DROP TABLE "membership" CASCADE']
    client.executed.clear()

    await DeleteModel(name="Person").run("models", state, dry_run=False, state_editor=editor)
    assert client.executed == ['DROP TABLE "person" CASCADE']


@pytest.mark.asyncio
async def test_add_index_operation_runs_sql() -> None:
    client = FakeClient("sql")
    editor = TestSchemaEditor(client)
    state = State(models={}, apps=StateApps())
    CreateModel(name="Widget", fields=[("id", fields.IntField(primary_key=True))]).state_forward("models", state)

    op = AddIndex(
        model_name="Widget",
        index=Index(fields=("id",), name="idx_widget_id"),
    )

    await op.run("models", state, dry_run=False, state_editor=editor)

    assert len(client.executed) == 1
    assert client.executed[0] == 'CREATE INDEX "idx_widget_id" ON "widget" ("id");'


@pytest.mark.asyncio
async def test_remove_index_operation_runs_sql() -> None:
    client = FakeClient("sql")
    editor = TestSchemaEditor(client)
    state = State(models={}, apps=StateApps())
    CreateModel(name="Widget", fields=[("id", fields.IntField(primary_key=True))]).state_forward("models", state)
    AddIndex(
        model_name="Widget",
        index=Index(fields=("id",), name="idx_widget_id"),
    ).state_forward("models", state)

    op = RemoveIndex(model_name="Widget", name="idx_widget_id")

    await op.run("models", state, dry_run=False, state_editor=editor)

    assert len(client.executed) == 1
    assert client.executed[0] == 'DROP INDEX "idx_widget_id"'


@pytest.mark.asyncio
async def test_rename_index_operation_runs_sql() -> None:
    client = FakeClient("sql")
    editor = TestSchemaEditor(client)
    state = State(models={}, apps=StateApps())
    CreateModel(name="Widget", fields=[("id", fields.IntField(primary_key=True))]).state_forward("models", state)
    AddIndex(
        model_name="Widget",
        index=Index(fields=("id",), name="idx_widget_id"),
    ).state_forward("models", state)

    op = RenameIndex(model_name="Widget", old_name="idx_widget_id", new_name="idx_widget_id_new")

    await op.run("models", state, dry_run=False, state_editor=editor)

    assert client.executed == ['DROP INDEX "idx_widget_id"', 'CREATE INDEX "idx_widget_id_new" ON "widget" ("id");']


@pytest.mark.asyncio
async def test_add_constraint_operation_runs_sql() -> None:
    client = FakeClient("sql")
    editor = TestSchemaEditor(client)
    state = State(models={}, apps=StateApps())
    CreateModel(name="Widget", fields=[("id", fields.IntField(primary_key=True))]).state_forward("models", state)

    op = AddConstraint(
        model_name="Widget",
        constraint=UniqueConstraint(fields=("id",), name="uniq_widget_id"),
    )

    await op.run("models", state, dry_run=False, state_editor=editor)

    assert len(client.executed) == 1
    assert client.executed[0] == 'ALTER TABLE "widget" ADD CONSTRAINT "uniq_widget_id" UNIQUE ("id")'


@pytest.mark.asyncio
async def test_alter_field_backward_renames_columns() -> None:
    client = FakeClient("sql")
    editor = TestSchemaEditor(client)

    OldWidget = make_model(
        "Widget",
        id=fields.IntField(primary_key=True),
        body=fields.TextField(),
    )
    NewWidget = make_model(
        "Widget",
        id=fields.IntField(primary_key=True),
        body=fields.TextField(source_field="content"),
    )

    old_state = build_state("models", NewWidget)
    new_state = build_state("models", OldWidget)

    op = AlterField(model_name="Widget", name="body", field=fields.TextField())

    await op.database_backward("models", old_state, new_state, state_editor=editor)

    assert len(client.executed) == 1
    assert client.executed[0] == 'ALTER TABLE "widget" RENAME COLUMN "content" TO "body"'


@pytest.mark.asyncio
async def test_run_python_operation_runs_callable() -> None:
    calls: list[tuple[StateApps, BaseSchemaEditor]] = []
    client = FakeClient("sql")
    editor = TestSchemaEditor(client)
    model = make_model("Widget", id=fields.IntField(primary_key=True))
    state = build_state("models", model)

    def forward(apps: StateApps, schema_editor: BaseSchemaEditor) -> None:
        apps.get_model("models.Widget")
        calls.append((apps, schema_editor))

    op = RunPython(forward)

    await op.run("models", state, dry_run=False, state_editor=editor)

    assert len(client.executed) == 0
    assert len(calls) == 1
    apps, schema_editor = calls[0]
    assert schema_editor is editor
    apps.get_model("models.Widget")


@pytest.mark.asyncio
async def test_rename_constraint_backward_runs_sql() -> None:
    client = FakeClient("sql")
    editor = TestSchemaEditor(client)

    def build_widget_state(constraint_name: str) -> State:
        model = make_model(
            "Widget",
            id=fields.IntField(primary_key=True),
            code=fields.IntField(),
            meta_options={"constraints": (UniqueConstraint(fields=("code",), name=constraint_name),)},
        )
        return build_state("models", model)

    op = RenameConstraint(model_name="Widget", old_name="uniq_old", new_name="uniq_new")

    await op.database_backward(
        "models", build_widget_state("uniq_new"), build_widget_state("uniq_old"), state_editor=editor
    )

    assert len(client.executed) == 1
    assert client.executed[0] == 'ALTER TABLE "widget" RENAME CONSTRAINT "uniq_new" TO "uniq_old"'


@pytest.mark.asyncio
async def test_create_model_uses_inline_unique() -> None:
    """CREATE TABLE should use inline UNIQUE for unique fields."""
    OldModel = make_model(
        "CryptoWallet",
        meta_options={"table": "crypto_wallets"},
        id=fields.IntField(primary_key=True),
        wallet_address=fields.CharField(max_length=255, unique=True, db_index=True),
    )

    create_client = FakeClient("sql")
    create_editor = TestSchemaEditor(create_client)
    await create_editor.create_model(OldModel)

    assert len(create_client.executed) == 1
    assert create_client.executed[0] == (
        'CREATE TABLE "crypto_wallets" (\n'
        '    "id" INT NOT NULL PRIMARY KEY,\n'
        '    "wallet_address" VARCHAR(255) NOT NULL UNIQUE\n'
        ");\n"
        'CREATE INDEX "idx_crypto_wall_wallet__06570f4760c9" ON "crypto_wallets" ("wallet_address");'
    )


@pytest.mark.asyncio
async def test_alter_field_unique_to_nonunique_generates_drop_constraint() -> None:
    """Changing unique=True to unique=False should produce a DROP CONSTRAINT."""
    OldModel = make_model(
        "CryptoWallet",
        meta_options={"table": "crypto_wallets"},
        id=fields.IntField(primary_key=True),
        wallet_address=fields.CharField(max_length=255, unique=True, db_index=True),
    )
    NewModel = make_model(
        "CryptoWallet",
        meta_options={"table": "crypto_wallets"},
        id=fields.IntField(primary_key=True),
        wallet_address=fields.CharField(max_length=255, unique=False, db_index=True),
    )

    old_state = build_state("models", OldModel)
    new_state = build_state("models", NewModel)

    alter_client = FakeClient("sql")
    alter_editor = TestSchemaEditor(alter_client)
    op = AlterField(
        model_name="CryptoWallet",
        name="wallet_address",
        field=fields.CharField(max_length=255, db_index=True),
    )
    await op.database_forward("models", old_state, new_state, state_editor=alter_editor)

    assert len(alter_client.executed) == 1
    assert alter_client.executed[0] == (
        'ALTER TABLE "crypto_wallets" DROP CONSTRAINT "uid_crypto_wall_wallet__06570f4760c9"'
    )


# ---------------------------------------------------------------------------
# Integration tests: AlterField with introspection-based constraint removal
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_postgres_alter_field_uses_introspected_legacy_name() -> None:
    """PostgreSQL AlterField unique=True->False should use the introspected constraint name."""
    OldModel = make_model(
        "CryptoWallet",
        meta_options={"table": "crypto_wallets"},
        id=fields.IntField(primary_key=True),
        wallet_address=fields.CharField(max_length=255, unique=True, db_index=True),
    )
    NewModel = make_model(
        "CryptoWallet",
        meta_options={"table": "crypto_wallets"},
        id=fields.IntField(primary_key=True),
        wallet_address=fields.CharField(max_length=255, unique=False, db_index=True),
    )

    old_state = build_state("models", OldModel)
    new_state = build_state("models", NewModel)

    client = MockIntrospectionClient(
        "postgresql",
        constraint_names=[{"conname": "crypto_wallets_wallet_address_key"}],
        inline_comment=False,
    )
    editor = PostgresqlSchemaEditor(client)
    op = AlterField(
        model_name="CryptoWallet",
        name="wallet_address",
        field=fields.CharField(max_length=255, db_index=True),
    )
    await op.database_forward("models", old_state, new_state, state_editor=editor)

    assert len(client.executed) == 1
    assert client.executed[0] == ('ALTER TABLE "crypto_wallets" DROP CONSTRAINT "crypto_wallets_wallet_address_key"')


def test_remove_field_error_message_includes_field_name() -> None:
    """RemoveField.state_forward() error should include the missing field name."""
    state = State(models={}, apps=StateApps())
    CreateModel(name="Widget", fields=[("id", fields.IntField(primary_key=True))]).state_forward("models", state)

    op = RemoveField(model_name="Widget", name="nonexistent_field")

    with pytest.raises(IncompatibleStateError, match="nonexistent_field"):
        op.state_forward("models", state)


# ---------------------------------------------------------------------------
# Step 8: Mock-based SQL verification tests for uncovered operations
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_remove_field_generates_sql() -> None:
    """RemoveField should generate ALTER TABLE ... DROP COLUMN SQL."""
    client = FakeClient("sql")
    editor = TestSchemaEditor(client)
    state = State(models={}, apps=StateApps())
    CreateModel(
        name="Widget",
        fields=[("id", fields.IntField(primary_key=True)), ("name", fields.TextField())],
    ).state_forward("models", state)

    op = RemoveField(model_name="Widget", name="name")

    await op.run("models", state, dry_run=False, state_editor=editor)

    assert len(client.executed) == 1
    assert client.executed[0] == 'ALTER TABLE "widget" DROP COLUMN "name" CASCADE'


@pytest.mark.asyncio
async def test_rename_model_generates_sql() -> None:
    """RenameModel should generate ALTER TABLE ... RENAME TO SQL when table name changes."""
    client = FakeClient("sql")
    editor = TestSchemaEditor(client)
    state = State(models={}, apps=StateApps())
    CreateModel(
        name="Widget",
        fields=[("id", fields.IntField(primary_key=True))],
    ).state_forward("models", state)

    op = RenameModel(old_name="Widget", new_name="Gadget")

    await op.run("models", state, dry_run=False, state_editor=editor)

    assert len(client.executed) == 1
    assert client.executed[0] == 'ALTER TABLE "widget" RENAME TO "gadget"'


@pytest.mark.asyncio
async def test_alter_field_forward_null_change() -> None:
    """AlterField null=False to null=True should DROP NOT NULL; reverse should SET NOT NULL."""
    # Forward: null=False -> null=True (DROP NOT NULL)
    OldModel = make_model(
        "Widget",
        id=fields.IntField(primary_key=True),
        name=fields.TextField(),
    )
    NewModel = make_model(
        "Widget",
        id=fields.IntField(primary_key=True),
        name=fields.TextField(null=True),
    )

    old_state = build_state("models", OldModel)
    new_state = build_state("models", NewModel)

    client = FakeClient("sql")
    editor = TestSchemaEditor(client)
    op = AlterField(model_name="Widget", name="name", field=fields.TextField(null=True))

    await op.database_forward("models", old_state, new_state, state_editor=editor)

    assert len(client.executed) == 1
    assert client.executed[0] == 'ALTER TABLE "widget" ALTER COLUMN "name" DROP NOT NULL'

    # Reverse: null=True -> null=False (SET NOT NULL)
    reverse_client = FakeClient("sql")
    reverse_editor = TestSchemaEditor(reverse_client)
    reverse_op = AlterField(model_name="Widget", name="name", field=fields.TextField())

    await reverse_op.database_forward("models", new_state, old_state, state_editor=reverse_editor)

    assert len(reverse_client.executed) == 1
    assert reverse_client.executed[0] == 'ALTER TABLE "widget" ALTER COLUMN "name" SET NOT NULL'


@pytest.mark.asyncio
async def test_alter_field_forward_db_default_change() -> None:
    """AlterField should SET DEFAULT when adding db_default and DROP DEFAULT when removing it."""
    # Forward: no db_default -> db_default=42 (SET DEFAULT)
    OldModel = make_model(
        "Widget",
        id=fields.IntField(primary_key=True),
        score=fields.IntField(),
    )
    NewModel = make_model(
        "Widget",
        id=fields.IntField(primary_key=True),
        score=fields.IntField(db_default=42),
    )

    old_state = build_state("models", OldModel)
    new_state = build_state("models", NewModel)

    client = FakeClient("sql")
    editor = TestSchemaEditor(client)
    op = AlterField(model_name="Widget", name="score", field=fields.IntField(db_default=42))

    await op.database_forward("models", old_state, new_state, state_editor=editor)

    assert len(client.executed) == 1
    assert client.executed[0] == 'ALTER TABLE "widget" ALTER COLUMN "score" SET DEFAULT 42'

    # Reverse: db_default=42 -> no db_default (DROP DEFAULT)
    drop_client = FakeClient("sql")
    drop_editor = TestSchemaEditor(drop_client)
    drop_op = AlterField(model_name="Widget", name="score", field=fields.IntField())

    await drop_op.database_forward("models", new_state, old_state, state_editor=drop_editor)

    assert len(drop_client.executed) == 1
    assert drop_client.executed[0] == 'ALTER TABLE "widget" ALTER COLUMN "score" DROP DEFAULT'


@pytest.mark.asyncio
async def test_run_sql_forward_and_backward() -> None:
    """RunSQL should execute forward SQL and reverse SQL."""
    client = FakeClient("sql")
    editor = TestSchemaEditor(client)
    state = State(models={}, apps=StateApps())

    op = RunSQL(
        'CREATE INDEX "idx_widget_name" ON "widget" ("name")',
        reverse_sql='DROP INDEX "idx_widget_name"',
    )

    # Forward
    await op.run("models", state, dry_run=False, state_editor=editor)

    assert len(client.executed) == 1
    assert client.executed[0] == 'CREATE INDEX "idx_widget_name" ON "widget" ("name")'

    # Backward
    backward_client = FakeClient("sql")
    backward_editor = TestSchemaEditor(backward_client)

    await op.database_backward("models", state, state, state_editor=backward_editor)

    assert len(backward_client.executed) == 1
    assert backward_client.executed[0] == 'DROP INDEX "idx_widget_name"'


@pytest.mark.asyncio
async def test_rename_constraint_uses_introspected_legacy_name() -> None:
    """rename_constraint()'s old_name used _constraint_name_for_model() directly (the
    deterministic uid_ hash convention) - unlike remove_constraint(), which already resolves the
    OLD constraint's name via DB introspection first, falling back to the deterministic name only
    when introspection can't find it. A legacy/hand-created constraint whose real name doesn't
    match the hash convention would make the generated RENAME CONSTRAINT SQL reference a name
    that doesn't exist in the DB."""
    Widget = make_model("Widget", id=fields.IntField(primary_key=True), code=fields.IntField())

    client = MockIntrospectionClient(
        "postgresql",
        constraint_names=[{"conname": "legacy_widget_code_unique"}],
        inline_comment=False,
    )
    editor = PostgresqlSchemaEditor(client)

    await editor.rename_constraint(
        Widget,
        UniqueConstraint(fields=("code",)),
        UniqueConstraint(fields=("code",), name="widget_code_unique_new"),
    )
    assert client.executed == [
        'ALTER TABLE "widget" RENAME CONSTRAINT "legacy_widget_code_unique" TO "widget_code_unique_new"'
    ]
