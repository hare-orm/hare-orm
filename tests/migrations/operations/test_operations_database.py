from __future__ import annotations

import re
from typing import Any, cast

import pytest

from hare import fields
from hare.ddl import RawSQLTerm
from hare.ddl.constraints import CheckConstraint, UniqueConstraint
from hare.ddl.indexes import Index
from hare.dialects.base.schema.base_schema_editor import BaseSchemaEditor
from hare.dialects.base.schema.tables.table_comments import TableComments
from hare.fields.field import Field
from hare.fields.relations.fields import ForeignKeyFieldInstance
from hare.fields.relations.fields.many_to_many_field_instance import ManyToManyFieldInstance
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
from hare.migrations.state.model_state import ModelState
from hare.migrations.state.state import State
from hare.migrations.state.state_apps import StateApps
from hare.models import Model
from tests.utils.fake_client import FakeClient
from tests.utils.operations import apply_operations

pytestmark = pytest.mark.database_independent


class TestSchemaEditorTableComments(TableComments):
    __test__ = False

    def get_table_comment_sql(self, table: str, comment: str) -> str:
        return ""

    def get_column_comment_sql(self, table: str, column: str, comment: str) -> str:
        return ""


class TestSchemaEditor(BaseSchemaEditor):
    __test__ = False

    table_comments_class = TestSchemaEditorTableComments


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
    return State(models={(app_label, model.__name__): ModelState.make_from_model(app_label, model)})


@pytest.mark.asyncio
async def test_create_model_operation_runs_sql() -> None:
    client = FakeClient("sql")
    editor = TestSchemaEditor(client)
    state = State()

    op = CreateModel(
        name="Widget",
        fields=[("id", fields.IntField(primary_key=True)), ("name", fields.TextField())],
    )

    await apply_operations("models", state, op, schema_editor=editor)

    assert len(client.executed) == 1
    assert client.executed[0] == (
        'CREATE TABLE "widget" (\n    "id" INT NOT NULL CONSTRAINT "pk_widget_id_35a4d654db7c" PRIMARY KEY,\n'
        '    "name" TEXT NOT NULL\n);'
    )


@pytest.mark.asyncio
async def test_create_model_operation_emits_check_and_unique_constraint_ddl() -> None:
    """Meta.constraints used to be recorded only in the diffing state, and NEVER translated into
    DDL when creating a NEW model - only add_constraint() on an already-existing one via
    AddConstraint. Regression test for BaseSchemaEditor.create_model()."""
    client = FakeClient("sql")
    editor = TestSchemaEditor(client)
    state = State()

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

    await apply_operations("models", state, op, schema_editor=editor)

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
    state = State()

    op = CreateModel(
        name="Widget",
        fields=[("id", fields.IntField(primary_key=True)), ("price", fields.IntField())],
        options={"indexes": (Index(fields=("price",), unique=True, name="uq_idx_widget_price"),)},
    )

    await apply_operations("models", state, op, schema_editor=editor)

    assert len(client.executed) == 1
    assert 'CREATE UNIQUE INDEX "uq_idx_widget_price" ON "widget" ("price")' in client.executed[0]


@pytest.mark.asyncio
async def test_add_field_operation_runs_sql() -> None:
    client = FakeClient("sql")
    editor = TestSchemaEditor(client)
    state = State()
    CreateModel(name="Widget", fields=[("id", fields.IntField(primary_key=True))]).state_forward("models", state)

    op = AddField(model_name="Widget", name="name", field=fields.TextField())

    await apply_operations("models", state, op, schema_editor=editor)

    assert len(client.executed) == 1
    assert client.executed[0] == 'ALTER TABLE "widget" ADD COLUMN "name" TEXT NOT NULL'


@pytest.mark.asyncio
async def test_delete_model_operation_runs_sql() -> None:
    client = FakeClient("sql")
    editor = TestSchemaEditor(client)
    state = State()
    CreateModel(name="Widget", fields=[("id", fields.IntField(primary_key=True))]).state_forward("models", state)

    op = DeleteModel(name="Widget")

    await apply_operations("models", state, op, schema_editor=editor)

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
    state = State()

    # One migration - the relation's target and through model come with it.
    await apply_operations(
        "models",
        state,
        CreateModel(
            name="Person",
            fields=[
                ("id", fields.IntField(primary_key=True)),
                (
                    "groups",
                    ManyToManyFieldInstance("models.Group", through="models.Membership", related_name="members"),
                ),
            ],
        ),
        CreateModel(name="Group", fields=[("id", fields.IntField(primary_key=True))]),
        CreateModel(
            name="Membership",
            fields=[
                ("id", fields.IntField(primary_key=True)),
                ("person", ForeignKeyFieldInstance("models.Person", related_name="membership_rows")),
                ("group", ForeignKeyFieldInstance("models.Group", related_name="membership_rows")),
            ],
        ),
        schema_editor=editor,
    )
    assert client.executed[0] == (
        'CREATE TABLE "person" (\n    "id" INT NOT NULL CONSTRAINT "pk_person_id_a6382eb6eef1" PRIMARY KEY\n);'
    )
    # Exactly one CREATE TABLE for "membership" itself - no separate opaque M2M through table.
    (membership_sql,) = [statement for statement in client.executed if "membership" in statement]
    assert 'CREATE TABLE "membership"' in membership_sql
    assert re.search(r'"person_id" INT NOT NULL CONSTRAINT "fk_\w+" REFERENCES "person"', membership_sql)
    assert re.search(r'"group_id" INT NOT NULL CONSTRAINT "fk_\w+" REFERENCES "group"', membership_sql)
    assert len(client.executed) == 3
    client.executed.clear()

    person = state.apps.get_model("models.Person")
    m2m_field = cast(ManyToManyFieldInstance, person._meta.fields_map["groups"])
    assert m2m_field.through == "membership"
    assert m2m_field.forward_keys == ("group_id",)
    assert m2m_field.backward_keys == ("person_id",)

    # AddField on the through model itself (an ordinary extra column) - normal ADD COLUMN DDL.
    await apply_operations(
        "models",
        state,
        AddField(model_name="Membership", name="note", field=fields.CharField(max_length=10, null=True)),
        schema_editor=editor,
    )
    assert client.executed == ['ALTER TABLE "membership" ADD COLUMN "note" VARCHAR(10)']
    client.executed.clear()

    # Removing the M2M field itself must NOT drop the through model's table - it's still a real,
    # independently-tracked model.
    await apply_operations("models", state, RemoveField(model_name="Person", name="groups"), schema_editor=editor)
    assert client.executed == []
    client.executed.clear()

    # Deleting the through model itself IS a real DROP TABLE - via its own DeleteModel.
    await apply_operations("models", state, DeleteModel(name="Membership"), schema_editor=editor)
    assert client.executed == ['DROP TABLE "membership" CASCADE']


@pytest.mark.asyncio
async def test_delete_model_with_m2m_through_model_does_not_redundantly_drop_through_table() -> None:
    """Deleting the M2M field's owning model, AFTER its through model was already dropped by its
    own separate DeleteModel, must not try to drop that table a second time."""
    client = FakeClient("sql")
    editor = TestSchemaEditor(client)
    state = State()

    await apply_operations(
        "models",
        state,
        CreateModel(
            name="Person",
            fields=[
                ("id", fields.IntField(primary_key=True)),
                (
                    "groups",
                    ManyToManyFieldInstance("models.Group", through="models.Membership", related_name="members"),
                ),
            ],
        ),
        CreateModel(name="Group", fields=[("id", fields.IntField(primary_key=True))]),
        CreateModel(
            name="Membership",
            fields=[
                ("id", fields.IntField(primary_key=True)),
                ("person", ForeignKeyFieldInstance("models.Person", related_name="membership_rows")),
                ("group", ForeignKeyFieldInstance("models.Group", related_name="membership_rows")),
            ],
        ),
        schema_editor=editor,
    )
    client.executed.clear()

    # One migration - the relation through the model goes with its owner.
    await apply_operations(
        "models", state, DeleteModel(name="Membership"), DeleteModel(name="Person"), schema_editor=editor
    )
    assert client.executed == ['DROP TABLE "membership" CASCADE', 'DROP TABLE "person" CASCADE']


@pytest.mark.parametrize(
    ("make_operation", "expected"),
    [
        pytest.param(
            lambda: AddIndex(model_name="Widget", index=Index(fields=("id",), name="idx_widget_id")),
            'CREATE INDEX "idx_widget_id" ON "widget" ("id");',
            id="add_index_operation_runs_sql",
        ),
        pytest.param(
            lambda: AddConstraint(
                model_name="Widget", constraint=UniqueConstraint(fields=("id",), name="uniq_widget_id")
            ),
            'ALTER TABLE "widget" ADD CONSTRAINT "uniq_widget_id" UNIQUE ("id")',
            id="add_constraint_operation_runs_sql",
        ),
    ],
)
@pytest.mark.asyncio
async def test_an_add_operation_runs_sql(make_operation, expected) -> None:
    client = FakeClient("sql")
    editor = TestSchemaEditor(client)
    state = State()
    CreateModel(name="Widget", fields=[("id", fields.IntField(primary_key=True))]).state_forward("models", state)

    await apply_operations("models", state, make_operation(), schema_editor=editor)

    assert client.executed == [expected]


@pytest.mark.asyncio
async def test_remove_index_operation_runs_sql() -> None:
    client = FakeClient("sql")
    editor = TestSchemaEditor(client)
    state = State()
    CreateModel(name="Widget", fields=[("id", fields.IntField(primary_key=True))]).state_forward("models", state)
    AddIndex(
        model_name="Widget",
        index=Index(fields=("id",), name="idx_widget_id"),
    ).state_forward("models", state)

    op = RemoveIndex(model_name="Widget", name="idx_widget_id")

    await apply_operations("models", state, op, schema_editor=editor)

    assert len(client.executed) == 1
    assert client.executed[0] == 'DROP INDEX "idx_widget_id"'


@pytest.mark.asyncio
async def test_rename_index_operation_runs_sql() -> None:
    client = FakeClient("sql")
    editor = TestSchemaEditor(client)
    state = State()
    CreateModel(name="Widget", fields=[("id", fields.IntField(primary_key=True))]).state_forward("models", state)
    AddIndex(
        model_name="Widget",
        index=Index(fields=("id",), name="idx_widget_id"),
    ).state_forward("models", state)

    op = RenameIndex(model_name="Widget", old_name="idx_widget_id", new_name="idx_widget_id_new")

    await apply_operations("models", state, op, schema_editor=editor)

    assert client.executed == ['DROP INDEX "idx_widget_id"', 'CREATE INDEX "idx_widget_id_new" ON "widget" ("id");']


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

    await apply_operations("models", state, op, schema_editor=editor)

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
async def test_create_model_names_the_unique_constraint_of_a_unique_field() -> None:
    """CREATE TABLE gives a unique field's constraint hare's name - never one the database makes up."""
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
        '    "id" INT NOT NULL CONSTRAINT "pk_crypto_wall_id_867f52498605" PRIMARY KEY,\n'
        '    "wallet_address" VARCHAR(255) NOT NULL,\n'
        '    CONSTRAINT "uid_crypto_wall_wallet__06570f4760c9" UNIQUE ("wallet_address")\n'
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


def test_remove_field_error_message_includes_field_name() -> None:
    """RemoveField.state_forward() error should include the missing field name."""
    state = State()
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
    state = State()
    CreateModel(
        name="Widget",
        fields=[("id", fields.IntField(primary_key=True)), ("name", fields.TextField())],
    ).state_forward("models", state)

    op = RemoveField(model_name="Widget", name="name")

    await apply_operations("models", state, op, schema_editor=editor)

    assert len(client.executed) == 1
    assert client.executed[0] == 'ALTER TABLE "widget" DROP COLUMN "name" CASCADE'


@pytest.mark.asyncio
async def test_rename_model_generates_sql() -> None:
    """RenameModel should generate ALTER TABLE ... RENAME TO SQL when table name changes."""
    client = FakeClient("sql")
    editor = TestSchemaEditor(client)
    state = State()
    CreateModel(
        name="Widget",
        fields=[("id", fields.IntField(primary_key=True))],
    ).state_forward("models", state)

    op = RenameModel(old_name="Widget", new_name="Gadget")

    await apply_operations("models", state, op, schema_editor=editor)

    # The primary key is named after the table - it takes the new table's name.
    assert client.executed == [
        'ALTER TABLE "widget" RENAME TO "gadget"',
        'ALTER TABLE "gadget" RENAME CONSTRAINT "pk_widget_id_35a4d654db7c" TO "pk_gadget_id_78926d8f092b"',
    ]


@pytest.mark.parametrize(
    ("field_name", "make_old_field", "make_new_field", "forward_sql", "reverse_sql"),
    [
        # null=False to null=True should DROP NOT NULL; reverse should SET NOT NULL.
        pytest.param(
            "name",
            lambda: fields.TextField(),
            lambda: fields.TextField(null=True),
            'ALTER TABLE "widget" ALTER COLUMN "name" DROP NOT NULL',
            'ALTER TABLE "widget" ALTER COLUMN "name" SET NOT NULL',
            id="alter_field_forward_null_change",
        ),
        # Adding db_default should SET DEFAULT, removing it DROP DEFAULT.
        pytest.param(
            "score",
            lambda: fields.IntField(),
            lambda: fields.IntField(db_default=42),
            'ALTER TABLE "widget" ALTER COLUMN "score" SET DEFAULT 42',
            'ALTER TABLE "widget" ALTER COLUMN "score" DROP DEFAULT',
            id="alter_field_forward_db_default_change",
        ),
    ],
)
@pytest.mark.asyncio
async def test_alter_field_forward_and_reverse(
    field_name, make_old_field, make_new_field, forward_sql, reverse_sql
) -> None:
    OldModel = make_model("Widget", id=fields.IntField(primary_key=True), **{field_name: make_old_field()})
    NewModel = make_model("Widget", id=fields.IntField(primary_key=True), **{field_name: make_new_field()})

    old_state = build_state("models", OldModel)
    new_state = build_state("models", NewModel)

    client = FakeClient("sql")
    editor = TestSchemaEditor(client)
    op = AlterField(model_name="Widget", name=field_name, field=make_new_field())

    await op.database_forward("models", old_state, new_state, state_editor=editor)

    assert client.executed == [forward_sql]

    reverse_client = FakeClient("sql")
    reverse_editor = TestSchemaEditor(reverse_client)
    reverse_op = AlterField(model_name="Widget", name=field_name, field=make_old_field())

    await reverse_op.database_forward("models", new_state, old_state, state_editor=reverse_editor)

    assert reverse_client.executed == [reverse_sql]


@pytest.mark.asyncio
async def test_run_sql_forward_and_backward() -> None:
    """RunSQL should execute forward SQL and reverse SQL."""
    client = FakeClient("sql")
    editor = TestSchemaEditor(client)
    state = State()

    op = RunSQL(
        'CREATE INDEX "idx_widget_name" ON "widget" ("name")',
        reverse_sql='DROP INDEX "idx_widget_name"',
    )

    # Forward
    await apply_operations("models", state, op, schema_editor=editor)

    assert len(client.executed) == 1
    assert client.executed[0] == 'CREATE INDEX "idx_widget_name" ON "widget" ("name")'

    # Backward
    backward_client = FakeClient("sql")
    backward_editor = TestSchemaEditor(backward_client)

    await op.database_backward("models", state, state, state_editor=backward_editor)

    assert len(backward_client.executed) == 1
    assert backward_client.executed[0] == 'DROP INDEX "idx_widget_name"'
