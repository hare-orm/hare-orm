from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest

from hare import fields
from hare.ddl import RawSQLTerm
from hare.ddl.constraints import CheckConstraint, ExclusionConstraint, UniqueConstraint
from hare.ddl.enums import TriggerEvent
from hare.ddl.indexes import Index
from hare.ddl.triggers import Trigger
from hare.dialects.postgresql.fields.search import TSVectorField
from hare.dialects.postgresql.indexes import GinIndex
from hare.fields.base import Field
from hare.fields.generated import GeneratedField
from hare.migrations.autodetection.operation_generator import OperationGenerator
from hare.migrations.operations import (
    AddConstraint,
    AddField,
    AddIndex,
    AddTrigger,
    AlterField,
    AlterModelSchema,
    AlterModelTable,
    AlterTrigger,
    CreateExtension,
    CreateModel,
    CreateSchema,
    DeleteModel,
    RemoveConstraint,
    RemoveExtension,
    RemoveField,
    RemoveIndex,
    RemoveTrigger,
    RenameConstraint,
    RenameField,
    RenameIndex,
    RenameModel,
    RenameTrigger,
)
from hare.migrations.state.apps import StateApps
from hare.migrations.state.project import ModelState, State
from hare.models import Model
from hare.query.expressions import F


def build_state(app_label: str, *models: type[Model]) -> State:
    state = State(models={}, apps=StateApps())
    for model in models:
        state.models[(app_label, model.__name__)] = ModelState.make_from_model(app_label, model)
    return state


def make_model(
    model_name: str,
    table: str,
    meta_options: Mapping[str, Any] | None = None,
    /,
    **model_fields: Field,
) -> type[Model]:
    attrs: dict[str, Any] = dict(model_fields)
    options: dict[str, Any] = {"app": "models", "table": table}
    if meta_options:
        options.update(meta_options)
    meta = type("Meta", (), options)
    attrs["Meta"] = meta
    return type(model_name, (Model,), attrs)


def make_text_field(source_field: str | None) -> fields.TextField:
    if source_field is None:
        return fields.TextField()
    return fields.TextField(source_field=source_field)


def test_generate_create_and_delete_model() -> None:
    Widget = make_model("Widget", "widget", id=fields.IntField(primary_key=True), name=fields.TextField())

    old_state = build_state("models")
    new_state = build_state("models", Widget)

    operations = OperationGenerator(old_state, new_state).generate()
    assert len(operations) == 1
    assert isinstance(operations[0], CreateModel)

    operations = OperationGenerator(new_state, old_state).generate()
    assert len(operations) == 1
    assert isinstance(operations[0], DeleteModel)


def test_generate_rename_model_heuristic() -> None:
    OldWidget = make_model("OldWidget", "widget", id=fields.IntField(primary_key=True), name=fields.TextField())
    NewWidget = make_model("NewWidget", "widget", id=fields.IntField(primary_key=True), name=fields.TextField())

    old_state = build_state("models", OldWidget)
    new_state = build_state("models", NewWidget)

    operations = OperationGenerator(old_state, new_state).generate()
    assert len(operations) == 1
    assert isinstance(operations[0], RenameModel)


def test_generate_alter_model_table_for_table_only_change() -> None:
    """A Meta.table-only change (same Python class name) used to be invisible to the
    autodetector entirely - "table" is deliberately excluded from the generic AlterModelOptions
    diff (whose database_forward/backward are no-ops), and RenameModel only fires on a class-name
    change, so nothing detected it and zero operations were generated."""
    Widget = make_model("Widget", "old_table", id=fields.IntField(primary_key=True))
    WidgetRenamedTable = make_model("Widget", "new_table", id=fields.IntField(primary_key=True))

    old_state = build_state("models", Widget)
    new_state = build_state("models", WidgetRenamedTable)

    operations = OperationGenerator(old_state, new_state).generate()
    assert len(operations) == 1
    assert isinstance(operations[0], AlterModelTable)
    assert operations[0].name == "Widget"
    assert operations[0].table == "new_table"


def test_generate_alter_model_schema_for_schema_only_change() -> None:
    """Same class of bug as test_generate_alter_model_table_for_table_only_change above, for
    Meta.schema instead of Meta.table - a schema-only change used to be invisible to the
    autodetector entirely (falls into the generic AlterModelOptions diff, whose database_forward/
    backward are unconditional no-ops), silently generating a migration that LOOKS like it moved
    the table to a new schema but never actually touches the database - the real table (and its
    data) stayed behind in the old schema forever."""
    Widget = make_model("Widget", "widget", id=fields.IntField(primary_key=True))
    WidgetMovedSchema = make_model(
        "Widget", "widget", {"schema": "other_schema"}, id=fields.IntField(primary_key=True)
    )

    old_state = build_state("models", Widget)
    new_state = build_state("models", WidgetMovedSchema)

    operations = OperationGenerator(old_state, new_state).generate()
    assert any(isinstance(op, CreateSchema) and op.schema_name == "other_schema" for op in operations)
    schema_ops = [op for op in operations if isinstance(op, AlterModelSchema)]
    assert len(schema_ops) == 1
    assert schema_ops[0].name == "Widget"
    assert schema_ops[0].schema == "other_schema"


def test_field_unique_and_matching_unique_together_added_together_skips_redundant_add_constraint() -> None:
    """A field's own unique=True flip AND a matching single-field unique_together entry for that
    SAME field, added in the same change, used to independently generate an AlterField AND an
    AddConstraint - both create the identical UNIQUE constraint (same deterministic name, a pure
    hash of table+fields), so applying both crashed with a duplicate-constraint error on any
    backend that does a real in-place ALTER (Postgres; masked on SQLite, whose alter_field
    rebuilds the whole table instead). The redundant AddConstraint must be skipped - AlterField's
    own DDL already creates the same constraint."""
    OldUser = make_model("User", "user", id=fields.IntField(primary_key=True), email=fields.CharField(64))
    NewUser = make_model(
        "User",
        "user",
        {"constraints": [UniqueConstraint(fields=("email",))]},
        id=fields.IntField(primary_key=True),
        email=fields.CharField(64, unique=True),
    )

    old_state = build_state("models", OldUser)
    new_state = build_state("models", NewUser)
    operations = OperationGenerator(old_state, new_state).generate()

    assert not any(isinstance(op, AddConstraint) for op in operations)
    alter_field_ops = [op for op in operations if isinstance(op, AlterField)]
    assert len(alter_field_ops) == 1
    assert alter_field_ops[0].field.unique is True


def test_field_unique_and_unrelated_unique_together_both_generate_operations() -> None:
    """The new guard must not become overzealous - a unique_together entry covering a DIFFERENT
    field than the one whose unique= flipped must still generate its own AddConstraint."""
    OldUser = make_model(
        "User", "user", id=fields.IntField(primary_key=True), email=fields.CharField(64), nickname=fields.CharField(64)
    )
    NewUser = make_model(
        "User",
        "user",
        {"constraints": [UniqueConstraint(fields=("nickname",))]},
        id=fields.IntField(primary_key=True),
        email=fields.CharField(64, unique=True),
        nickname=fields.CharField(64),
    )

    old_state = build_state("models", OldUser)
    new_state = build_state("models", NewUser)
    operations = OperationGenerator(old_state, new_state).generate()

    add_constraint_ops = [op for op in operations if isinstance(op, AddConstraint)]
    assert len(add_constraint_ops) == 1
    assert tuple(add_constraint_ops[0].constraint.fields) == ("nickname",)
    alter_field_ops = [op for op in operations if isinstance(op, AlterField)]
    assert len(alter_field_ops) == 1
    assert alter_field_ops[0].name == "email"


def test_generate_field_ops() -> None:
    OldWidget = make_model("Widget", "widget", id=fields.IntField(primary_key=True), name=fields.TextField())
    NewWidget = make_model(
        "Widget",
        "widget",
        id=fields.IntField(primary_key=True),
        title=fields.TextField(source_field="name"),
        age=fields.IntField(),
    )

    old_state = build_state("models", OldWidget)
    new_state = build_state("models", NewWidget)

    operations = OperationGenerator(old_state, new_state).generate()
    assert any(isinstance(op, RenameField) for op in operations)
    assert any(isinstance(op, AddField) for op in operations)


def test_added_indexed_field_gets_a_companion_add_index() -> None:
    """AddField only ever creates the column itself - a brand new db_index=True field
    added to an existing table used to silently get no index at all, since index creation
    otherwise only happens as part of the initial CREATE TABLE."""
    OldWidget = make_model("Widget", "widget", id=fields.IntField(primary_key=True))
    NewWidget = make_model(
        "Widget",
        "widget",
        id=fields.IntField(primary_key=True),
        name=fields.CharField(max_length=50, db_index=True),
    )

    old_state = build_state("models", OldWidget)
    new_state = build_state("models", NewWidget)

    operations = OperationGenerator(old_state, new_state).generate()
    assert any(isinstance(op, AddField) and op.name == "name" for op in operations)
    add_index_ops = [op for op in operations if isinstance(op, AddIndex)]
    assert len(add_index_ops) == 1
    assert list(add_index_ops[0].index.fields) == ["name"]


def test_added_non_indexed_field_gets_no_add_index() -> None:
    """Regression anchor - a plain (non-indexed) added field must not spuriously get an
    AddIndex."""
    OldWidget = make_model("Widget", "widget", id=fields.IntField(primary_key=True))
    NewWidget = make_model(
        "Widget", "widget", id=fields.IntField(primary_key=True), name=fields.CharField(max_length=50)
    )

    old_state = build_state("models", OldWidget)
    new_state = build_state("models", NewWidget)

    operations = OperationGenerator(old_state, new_state).generate()
    assert not any(isinstance(op, AddIndex) for op in operations)


def test_indexed_field_survives_a_second_makemigrations_run_with_no_changes() -> None:
    """Regression test: an AddField+AddIndex pair generated for a new db_index=True
    field used to permanently corrupt tracked migration state. AddIndex.state_forward() records
    a synthetic Index(fields=("name",)) into the tracked model's options["indexes"] - but the
    LIVE model's state (built fresh from the real model class on every makemigrations run) never
    carried an equivalent entry, since ModelState.make_from_model() only ever copied explicit
    Meta.indexes, never a field's own db_index=True. That made the tracked state look like it had
    an index the live model didn't have anymore, so the very next makemigrations run proposed a
    RemoveIndex - which, applied, actually dropped the real index while the model still declared
    db_index=True on the field: silent, permanent, undetectable index loss.

    Simulates the tracked state the way the real executor builds it: replaying CreateModel then
    AddField/AddIndex.state_forward() (not build_state(), which would call make_from_model()
    directly on a model that already has the field, sidestepping the synthetic-entry bookkeeping
    entirely and failing to reproduce the bug)."""
    Widget = make_model("Widget", "widget", id=fields.IntField(primary_key=True))
    widget_fields = list(build_state("models", Widget).models[("models", "Widget")].fields.items())
    create_op = CreateModel(name="Widget", fields=widget_fields)
    tracked_state = State(models={}, apps=StateApps())
    create_op.state_forward("models", tracked_state)

    name_field = fields.CharField(max_length=50, db_index=True)
    AddField(model_name="Widget", name="name", field=name_field).state_forward("models", tracked_state)
    AddIndex(model_name="Widget", index=Index(fields=("name",))).state_forward("models", tracked_state)

    WidgetWithIndexedName = make_model(
        "Widget", "widget", id=fields.IntField(primary_key=True), name=fields.CharField(max_length=50, db_index=True)
    )
    live_state = build_state("models", WidgetWithIndexedName)

    operations = OperationGenerator(tracked_state, live_state).generate()
    assert operations == []


@pytest.mark.parametrize(
    ("old_name", "new_name", "old_source", "new_source", "expect_rename", "expect_alter"),
    [
        ("title", "title", None, None, False, False),
        ("title", "title", None, "legacy_title", False, True),
        # No source_field signal pointing the new field at the old one - an unambiguous
        # type/nullable signature match alone is no longer enough to trust as a rename (it's
        # the same shape as two unrelated fields coincidentally sharing a signature), so this
        # falls back to remove+add rather than RenameField.
        ("title", "headline", None, None, False, False),
        ("title", "headline", None, "title", True, False),
    ],
)
def test_generate_rename_field_source_field_matrix(
    old_name: str,
    new_name: str,
    old_source: str | None,
    new_source: str | None,
    expect_rename: bool,
    expect_alter: bool,
) -> None:
    OldWidget = make_model(
        "Widget",
        "widget",
        id=fields.IntField(primary_key=True),
        **{old_name: make_text_field(old_source)},
    )
    NewWidget = make_model(
        "Widget",
        "widget",
        id=fields.IntField(primary_key=True),
        **{new_name: make_text_field(new_source)},
    )

    old_state = build_state("models", OldWidget)
    new_state = build_state("models", NewWidget)

    operations = OperationGenerator(old_state, new_state).generate()
    assert any(isinstance(op, RenameField) for op in operations) is expect_rename
    assert any(isinstance(op, AlterField) for op in operations) is expect_alter


def test_generate_two_fields_swapping_db_columns_raises() -> None:
    """Two fields keeping their own Python attribute names but swapping which DB column each one
    points at (field 'a' -> source_field='b', field 'b' -> source_field='a') used to generate two
    independent AlterField operations, each rendering its own ALTER TABLE ... RENAME COLUMN with
    no awareness of the other - the first one always collides with the column the second hasn't
    vacated yet ("duplicate column name"), crashing migrate on every real backend. Must raise a
    clear ConfigurationError at makemigrations time instead."""
    OldWidget = make_model(
        "Widget",
        "widget",
        id=fields.IntField(primary_key=True),
        a=make_text_field(None),
        b=make_text_field(None),
    )
    NewWidget = make_model(
        "Widget",
        "widget",
        id=fields.IntField(primary_key=True),
        a=make_text_field("b"),
        b=make_text_field("a"),
    )

    old_state = build_state("models", OldWidget)
    new_state = build_state("models", NewWidget)

    from hare.exceptions import ConfigurationError

    with pytest.raises(ConfigurationError, match="swapping DB columns"):
        OperationGenerator(old_state, new_state).generate()


def test_generate_rename_field_carries_the_new_fields_real_source_field() -> None:
    """The autodetector's rename heuristic only fires when the new field's own source_field
    explicitly names the removed field's db column - but RenameField used to be constructed
    with just old_name/new_name, discarding that new field entirely. state_forward() then moved
    the OLD field object under the new key, so the new field's real source_field (which may
    intentionally keep the DB column unchanged) never reached the tracked state at all. The
    generated RenameField must carry the live new field via field=, source_field intact."""
    OldWidget = make_model("Widget", "widget", id=fields.IntField(primary_key=True), title=make_text_field(None))
    NewWidget = make_model("Widget", "widget", id=fields.IntField(primary_key=True), headline=make_text_field("title"))

    old_state = build_state("models", OldWidget)
    new_state = build_state("models", NewWidget)

    operations = OperationGenerator(old_state, new_state).generate()
    rename_ops = [op for op in operations if isinstance(op, RenameField)]
    assert len(rename_ops) == 1
    assert rename_ops[0].field is not None
    assert rename_ops[0].field.source_field == "title"


def test_generate_field_ops_does_not_guess_rename_for_coincidental_signature_collision() -> None:
    """An unrelated field removed and a new field added in the same pass, sharing a type/
    nullable signature purely by coincidence (two plain nullable IntFields), must not be
    silently paired into a RenameField - that operation's RENAME COLUMN would carry the removed
    field's data under the added field's name with zero confirmation. Must produce a plain
    RemoveField + AddField instead - but WITH an advisory warning (OperationGenerator.warnings),
    since this exact shape (same signature, unrecognized as a rename) is also what a genuine
    rename-that-also-renames-its-column looks like from the autodetector's own point of view -
    see test_generate_rename_with_column_rename_is_not_recognized_but_warns below."""
    OldWidget = make_model("Widget", "widget", id=fields.IntField(primary_key=True), age=fields.IntField(null=True))
    NewWidget = make_model("Widget", "widget", id=fields.IntField(primary_key=True), score=fields.IntField(null=True))

    old_state = build_state("models", OldWidget)
    new_state = build_state("models", NewWidget)

    generator = OperationGenerator(old_state, new_state)
    operations = generator.generate()
    assert not any(isinstance(op, RenameField) for op in operations)
    assert any(isinstance(op, RemoveField) and op.name == "age" for op in operations)
    assert any(isinstance(op, AddField) and op.name == "score" for op in operations)
    assert len(generator.warnings) == 1
    assert "age" in generator.warnings[0]
    assert "score" in generator.warnings[0]


def test_generate_rename_with_column_rename_is_not_recognized_but_warns() -> None:
    """The rename heuristic only ever trusts a NEW field's source_field explicitly naming the
    REMOVED field's own db column (see StateFieldDiff.generate_operations()'s own comment) - a
    field renamed AND moved onto a differently-named column in the same edit fails that check on
    both sides (old_name != new_source_field, new_source_field != old column), so it falls
    through to a plain AddField + RemoveField, which - applied against a real table - drops the
    old column's data instead of carrying it over via RENAME COLUMN. Not automatically fixable
    without risking the opposite failure mode (misattributing an unrelated add+remove of the
    same type as a rename) - see the coincidental-collision test above, which hits the exact
    same code path. Must still warn, so the same-type case is at least not silently dangerous."""
    OldWidget = make_model("Widget", "widget", id=fields.IntField(primary_key=True), title=make_text_field(None))
    NewWidget = make_model(
        "Widget", "widget", id=fields.IntField(primary_key=True), headline=make_text_field("title_col")
    )

    old_state = build_state("models", OldWidget)
    new_state = build_state("models", NewWidget)

    generator = OperationGenerator(old_state, new_state)
    operations = generator.generate()
    assert not any(isinstance(op, RenameField) for op in operations)
    assert any(isinstance(op, RemoveField) and op.name == "title" for op in operations)
    assert any(isinstance(op, AddField) and op.name == "headline" for op in operations)
    assert len(generator.warnings) == 1
    assert "title" in generator.warnings[0]
    assert "headline" in generator.warnings[0]


def test_generate_rename_of_generated_field_into_plain_field_warns_about_dropped_values() -> None:
    """A rename detected via source_field that ALSO turns a GeneratedField into a plain field
    can't go through RenameField (see generate_operations()'s own comment on why - a generated
    column can't safely carry a type/attribute change through RenameField+AlterField) - it falls
    through to RemoveField+AddField instead, same as the column-rename case above, but this time
    RemoveField genuinely DROPS the old column's already-computed values with no way to carry
    them over (unlike a plain rename, RENAME COLUMN was never an option here to begin with).
    Confirmed live that this happened completely silently before this warning existed."""
    OldWidget = make_model(
        "Widget",
        "widget",
        id=fields.IntField(primary_key=True),
        total=GeneratedField(expression="price * quantity", output_field=fields.IntField(), source_field="total"),
    )
    NewWidget = make_model(
        "Widget", "widget", id=fields.IntField(primary_key=True), amount=fields.IntField(source_field="total")
    )

    old_state = build_state("models", OldWidget)
    new_state = build_state("models", NewWidget)

    generator = OperationGenerator(old_state, new_state)
    operations = generator.generate()
    assert not any(isinstance(op, RenameField) for op in operations)
    assert any(isinstance(op, RemoveField) and op.name == "total" for op in operations)
    assert any(isinstance(op, AddField) and op.name == "amount" for op in operations)
    assert not generator.warnings
    assert len(generator.data_loss_warnings) == 1
    assert "total" in generator.data_loss_warnings[0]
    assert "amount" in generator.data_loss_warnings[0]
    assert "DROP" in generator.data_loss_warnings[0]


def test_generate_same_name_generated_field_turned_plain_warns_about_dropped_values() -> None:
    """Same data-loss risk as the rename case above, but without any rename involved - a
    GeneratedField turned into a plain field under the SAME name still can't go through
    AlterField (same guard), and RemoveField+AddField still drops the old computed values."""
    OldWidget = make_model(
        "Widget",
        "widget",
        id=fields.IntField(primary_key=True),
        total=GeneratedField(expression="price * quantity", output_field=fields.IntField()),
    )
    NewWidget = make_model("Widget", "widget", id=fields.IntField(primary_key=True), total=fields.IntField())

    old_state = build_state("models", OldWidget)
    new_state = build_state("models", NewWidget)

    generator = OperationGenerator(old_state, new_state)
    operations = generator.generate()
    assert any(isinstance(op, RemoveField) and op.name == "total" for op in operations)
    assert any(isinstance(op, AddField) and op.name == "total" for op in operations)
    assert not generator.warnings
    assert len(generator.data_loss_warnings) == 1
    assert "total" in generator.data_loss_warnings[0]
    assert "DROP" in generator.data_loss_warnings[0]


def test_generate_alter_field() -> None:
    OldWidget = make_model("Widget", "widget", id=fields.IntField(primary_key=True), name=fields.TextField())
    NewWidget = make_model("Widget", "widget", id=fields.IntField(primary_key=True), name=fields.TextField(null=True))

    old_state = build_state("models", OldWidget)
    new_state = build_state("models", NewWidget)

    operations = OperationGenerator(old_state, new_state).generate()
    assert any(isinstance(op, AlterField) for op in operations)


def test_generate_alter_field_narrowing_max_length_warns_about_data_loss() -> None:
    """Narrowing CharField.max_length can silently truncate already-stored values once applied
    on Postgres (the schema editor's own runtime guard refuses it there if any row actually
    overflows) - makemigrations should already flag this ahead of time, the same way it already
    does for a generated field turning into a plain one."""
    OldWidget = make_model(
        "Widget", "widget", id=fields.IntField(primary_key=True), name=fields.CharField(max_length=100)
    )
    NewWidget = make_model(
        "Widget", "widget", id=fields.IntField(primary_key=True), name=fields.CharField(max_length=5)
    )

    old_state = build_state("models", OldWidget)
    new_state = build_state("models", NewWidget)

    generator = OperationGenerator(old_state, new_state)
    operations = generator.generate()
    assert any(isinstance(op, AlterField) and op.name == "name" for op in operations)
    assert len(generator.data_loss_warnings) == 1
    assert "name" in generator.data_loss_warnings[0]


def test_generate_alter_field_widening_max_length_does_not_warn() -> None:
    """Widening max_length never loses data - only a narrower bound needs the warning."""
    OldWidget = make_model(
        "Widget", "widget", id=fields.IntField(primary_key=True), name=fields.CharField(max_length=5)
    )
    NewWidget = make_model(
        "Widget", "widget", id=fields.IntField(primary_key=True), name=fields.CharField(max_length=100)
    )

    old_state = build_state("models", OldWidget)
    new_state = build_state("models", NewWidget)

    generator = OperationGenerator(old_state, new_state)
    operations = generator.generate()
    assert any(isinstance(op, AlterField) and op.name == "name" for op in operations)
    assert not generator.data_loss_warnings


def test_generate_rename_field_narrowing_decimal_places_warns_about_data_loss() -> None:
    """The same narrowing warning must also fire for a DecimalField rename+narrow, which goes
    through the RenameField+AlterField pair rather than a plain same-name AlterField."""
    OldWidget = make_model(
        "Widget",
        "widget",
        id=fields.IntField(primary_key=True),
        price=fields.DecimalField(max_digits=10, decimal_places=5),
    )
    NewWidget = make_model(
        "Widget",
        "widget",
        id=fields.IntField(primary_key=True),
        cost=fields.DecimalField(max_digits=10, decimal_places=2, source_field="price"),
    )

    old_state = build_state("models", OldWidget)
    new_state = build_state("models", NewWidget)

    generator = OperationGenerator(old_state, new_state)
    operations = generator.generate()
    assert any(isinstance(op, RenameField) for op in operations)
    assert any(isinstance(op, AlterField) and op.name == "cost" for op in operations)
    assert len(generator.data_loss_warnings) == 1
    assert "cost" in generator.data_loss_warnings[0]


def test_generate_fk_to_m2m_conversion_raises_clear_configuration_error() -> None:
    """Converting a field from a ForeignKeyField to a ManyToManyField (or vice versa) has no
    automatic migration path - the two have entirely different physical schemas (a column plus
    a constraint versus a separate through table). Before this had its own detection, the field
    diff just handed it to AlterField like any other type change, which then crashed
    ungracefully once applied - this must raise a clear ConfigurationError at generation time
    instead."""
    from hare.exceptions import ConfigurationError

    Owner = make_model("Owner", "owner", id=fields.IntField(primary_key=True))
    OldWidget = make_model(
        "Widget", "widget", id=fields.IntField(primary_key=True), owner=fields.ForeignKeyField("models.Owner")
    )
    NewWidget = make_model(
        "Widget", "widget", id=fields.IntField(primary_key=True), owner=fields.ManyToManyField("models.Owner")
    )

    old_state = build_state("models", Owner, OldWidget)
    new_state = build_state("models", Owner, NewWidget)

    with pytest.raises(ConfigurationError, match="ForeignKeyField.*ManyToManyField"):
        OperationGenerator(old_state, new_state).generate()


def test_generate_m2m_to_fk_conversion_raises_clear_configuration_error() -> None:
    """Sibling of the FK->M2M test above for the opposite direction."""
    from hare.exceptions import ConfigurationError

    Owner = make_model("Owner", "owner", id=fields.IntField(primary_key=True))
    OldWidget = make_model(
        "Widget", "widget", id=fields.IntField(primary_key=True), owner=fields.ManyToManyField("models.Owner")
    )
    NewWidget = make_model(
        "Widget", "widget", id=fields.IntField(primary_key=True), owner=fields.ForeignKeyField("models.Owner")
    )

    old_state = build_state("models", Owner, OldWidget)
    new_state = build_state("models", Owner, NewWidget)

    with pytest.raises(ConfigurationError, match="ForeignKeyField.*ManyToManyField"):
        OperationGenerator(old_state, new_state).generate()


def test_generate_fk_retarget_to_a_different_model_produces_alter_field() -> None:
    """Retargeting a ForeignKeyField at a different, unrelated existing model (not a rename of
    the target model itself - RenameModel already patches dependent fields' model_name in state
    separately) must be detected as a real field change. ForeignKeyFieldInstance.describe() used
    to omit model_name entirely (unlike ManyToManyFieldInstance.describe(), which always included
    it) - field_signature() compares describe()'s output verbatim, so the retarget was completely
    invisible to the autodetector and silently produced zero operations."""
    ModelA = make_model("ModelA", "model_a", id=fields.IntField(primary_key=True))
    ModelB = make_model("ModelB", "model_b", id=fields.IntField(primary_key=True))
    OldChild = make_model(
        "Child", "child", id=fields.IntField(primary_key=True), parent=fields.ForeignKeyField("models.ModelA")
    )
    NewChild = make_model(
        "Child", "child", id=fields.IntField(primary_key=True), parent=fields.ForeignKeyField("models.ModelB")
    )

    old_state = build_state("models", ModelA, ModelB, OldChild)
    new_state = build_state("models", ModelA, ModelB, NewChild)

    operations = OperationGenerator(old_state, new_state).generate()

    assert len(operations) == 1
    assert isinstance(operations[0], AlterField)
    assert operations[0].name == "parent"


def test_generate_m2m_field_rename_produces_rename_field_not_add_remove() -> None:
    """A ManyToManyField owns no column of its own (no source_field to match a rename on, unlike
    every other field type), so the generic rename-detection branch always skipped it, falling
    through to a plain AddField(new_name) + RemoveField(old_name) pair. For M2M, RemoveField
    DROPS the through table and AddField re-CREATES it under the identical name (through is
    derived purely from the two owning tables, unaffected by the Python-level attribute rename)
    - applying that migration crashes at state_forward() time, before any DDL, the moment
    AddField's own state re-derivation sees both the old and new field's auto-derived default
    related_name collide (both fields temporarily coexist in state) - see
    test_operations_real_db.py's end-to-end version of this same scenario for the live crash."""
    Tag = make_model("Tag", "tag", id=fields.IntField(primary_key=True))
    OldWidget = make_model(
        "Widget",
        "widget",
        id=fields.IntField(primary_key=True),
        tags=fields.ManyToManyField(
            "models.Tag", through="widget_tag", forward_key="tag_id", backward_key="widget_id"
        ),
    )
    NewWidget = make_model(
        "Widget",
        "widget",
        id=fields.IntField(primary_key=True),
        labels=fields.ManyToManyField(
            "models.Tag", through="widget_tag", forward_key="tag_id", backward_key="widget_id"
        ),
    )

    old_state = build_state("models", Tag, OldWidget)
    new_state = build_state("models", Tag, NewWidget)

    operations = OperationGenerator(old_state, new_state).generate()
    assert len(operations) == 1
    assert isinstance(operations[0], RenameField)
    assert operations[0].old_name == "tags"
    assert operations[0].new_name == "labels"

    # Applying it must not raise - the exact crash this fix prevents.
    tracked_state = build_state("models", Tag, OldWidget)
    for op in operations:
        op.state_forward("models", tracked_state)
    assert "labels" in tracked_state.models[("models", "Widget")].fields
    assert "tags" not in tracked_state.models[("models", "Widget")].fields


def test_generate_m2m_field_rename_combined_with_through_change_falls_back_to_add_remove() -> None:
    """A rename bundled with a genuine physical change (a different through table here) isn't a
    pure Python-level rename - field_signature_for_rename() correctly sees the two fields as
    different, so the ambiguous case falls through to the ordinary add+remove path rather than
    silently treating a real schema change as a costless rename."""
    Tag = make_model("Tag", "tag", id=fields.IntField(primary_key=True))
    OldWidget = make_model(
        "Widget",
        "widget",
        id=fields.IntField(primary_key=True),
        tags=fields.ManyToManyField(
            "models.Tag", through="widget_tag_old", forward_key="tag_id", backward_key="widget_id"
        ),
    )
    NewWidget = make_model(
        "Widget",
        "widget",
        id=fields.IntField(primary_key=True),
        labels=fields.ManyToManyField(
            "models.Tag", through="widget_tag_new", forward_key="tag_id", backward_key="widget_id"
        ),
    )

    old_state = build_state("models", Tag, OldWidget)
    new_state = build_state("models", Tag, NewWidget)

    operations = OperationGenerator(old_state, new_state).generate()
    assert not any(isinstance(op, RenameField) for op in operations)
    assert any(isinstance(op, AddField) and op.name == "labels" for op in operations)
    assert any(isinstance(op, RemoveField) and op.name == "tags" for op in operations)


def test_generate_pk_change_raises_instead_of_silent_noop() -> None:
    """Changing which field is the primary key has no dedicated operation -
    AlterModelOptions.database_forward/backward are unconditional no-ops, so this must raise
    instead of silently generating a migration that looks like it changed the PK but never
    touches the database, desyncing ORM state from the real schema."""
    OldWidget = make_model(
        "Widget", "widget", id=fields.IntField(primary_key=True), code=fields.CharField(max_length=20, unique=True)
    )
    NewWidget = make_model(
        "Widget", "widget", id=fields.IntField(), code=fields.CharField(max_length=20, primary_key=True)
    )

    old_state = build_state("models", OldWidget)
    new_state = build_state("models", NewWidget)

    with pytest.raises(Exception, match="primary key"):
        OperationGenerator(old_state, new_state).generate()


def test_generate_pk_rename_detected_instead_of_raising() -> None:
    """An unambiguous rename of the PK field itself (new field's source_field explicitly names
    the old field's db column, same signature otherwise) must be recognized by the rename
    heuristic and produce a RenameField op - not raise "changing the primary key ... is not
    supported", which used to fire unconditionally on any pk_field_name change before the
    rename heuristic ever got a chance to run."""
    OldWidget = make_model("Widget", "widget", id=fields.IntField(primary_key=True), name=fields.TextField())
    NewWidget = make_model(
        "Widget", "widget", pk_id=fields.IntField(primary_key=True, source_field="id"), name=fields.TextField()
    )

    old_state = build_state("models", OldWidget)
    new_state = build_state("models", NewWidget)

    operations = OperationGenerator(old_state, new_state).generate()
    assert len(operations) == 1
    rename_op = operations[0]
    assert isinstance(rename_op, RenameField)
    assert rename_op.old_name == "id"
    assert rename_op.new_name == "pk_id"


def test_generate_composite_pk_component_rename_detected_instead_of_raising() -> None:
    """Sibling of test_generate_pk_rename_detected_instead_of_raising for a COMPOSITE primary
    key - _pk_rename_operation() used to bail unconditionally the moment either side's
    pk_field_name was a tuple, never even checking whether field_operations already contained
    the correct, unambiguous RenameField for the one component that changed (the exact same
    heuristic already trusted for a single-column PK) - StateFieldDiff's own rename detection
    has no special-casing for a PK field either way, so it already produced the right operation;
    only the guard here was unconditionally throwing it away."""
    OldWidget = make_model(
        "Widget",
        "widget",
        a=fields.IntField(),
        b=fields.IntField(),
        name=fields.TextField(),
        pk=fields.CompositePrimaryKey("a", "b"),
    )
    NewWidget = make_model(
        "Widget",
        "widget",
        a=fields.IntField(),
        c=fields.IntField(source_field="b"),
        name=fields.TextField(),
        pk=fields.CompositePrimaryKey("a", "c"),
    )

    old_state = build_state("models", OldWidget)
    new_state = build_state("models", NewWidget)

    operations = OperationGenerator(old_state, new_state).generate()
    assert len(operations) == 1
    rename_op = operations[0]
    assert isinstance(rename_op, RenameField)
    assert rename_op.old_name == "b"
    assert rename_op.new_name == "c"


def test_generate_composite_pk_membership_change_still_raises() -> None:
    """The guard above must still fire for a GENUINE composite-PK shape change (not a clean
    1:1 rename of one component) - gaining a member here has no single RenameField that could
    account for it."""
    OldWidget = make_model(
        "Widget",
        "widget",
        a=fields.IntField(),
        b=fields.IntField(),
        name=fields.TextField(),
        pk=fields.CompositePrimaryKey("a", "b"),
    )
    NewWidget = make_model(
        "Widget",
        "widget",
        a=fields.IntField(),
        b=fields.IntField(),
        c=fields.IntField(),
        name=fields.TextField(),
        pk=fields.CompositePrimaryKey("a", "b", "c"),
    )

    old_state = build_state("models", OldWidget)
    new_state = build_state("models", NewWidget)

    with pytest.raises(Exception, match="primary key"):
        OperationGenerator(old_state, new_state).generate()


def test_generate_alter_field_when_generated_sql_changes() -> None:
    OldWidget = make_model(
        "Widget",
        "widget",
        id=fields.IntField(primary_key=True),
        title=fields.TextField(),
        body=fields.TextField(),
        search_vector=TSVectorField(source_fields=("title",), config="english"),
    )
    NewWidget = make_model(
        "Widget",
        "widget",
        id=fields.IntField(primary_key=True),
        title=fields.TextField(),
        body=fields.TextField(),
        search_vector=TSVectorField(source_fields=("title", "body"), config="english"),
    )

    old_state = build_state("models", OldWidget)
    new_state = build_state("models", NewWidget)

    operations = OperationGenerator(old_state, new_state).generate()
    assert any(isinstance(op, RemoveField) for op in operations)
    assert any(isinstance(op, AddField) for op in operations)


def test_generate_recreate_generated_field_restores_indexes_constraints() -> None:
    OldWidget = make_model(
        "Widget",
        "widget",
        id=fields.IntField(primary_key=True),
        title=fields.TextField(),
        body=fields.TextField(),
        search_vector=TSVectorField(source_fields=("title",), config="english", db_index=True),
    )
    NewWidget = make_model(
        "Widget",
        "widget",
        id=fields.IntField(primary_key=True),
        title=fields.TextField(),
        body=fields.TextField(),
        search_vector=TSVectorField(source_fields=("title", "body"), config="english", db_index=True),
    )
    OldWidget._meta.indexes = (GinIndex(fields=("search_vector",)),)
    OldWidget._meta.constraints = (UniqueConstraint(fields=("title", "search_vector")),)
    NewWidget._meta.indexes = (GinIndex(fields=("search_vector",)),)
    NewWidget._meta.constraints = (UniqueConstraint(fields=("title", "search_vector")),)

    old_state = build_state("models", OldWidget)
    new_state = build_state("models", NewWidget)

    operations = OperationGenerator(old_state, new_state).generate()
    assert any(isinstance(op, RemoveField) for op in operations)
    assert any(isinstance(op, AddField) for op in operations)
    assert any(isinstance(op, AddIndex) and op.index.INDEX_TYPE == "GIN" for op in operations)
    assert any(
        isinstance(op, AddConstraint)
        and isinstance(op.constraint, UniqueConstraint)
        and op.constraint.fields == ("title", "search_vector")
        for op in operations
    )


def test_generate_alter_field_for_an_unrelated_pk_attribute_change_does_not_drop_the_pk() -> None:
    """field.generated is True for a plain auto-increment pk (IntField's own __init__ sets it,
    for an entirely different reason than a real GeneratedField - a caller-supplied custom pk
    value needs to reach the INSERT column list) as well as for a real GeneratedField - the
    generated-field RemoveField+AddField branches above never excluded a pk field, so ANY
    unrelated attribute change on an auto-increment pk (description here, but equally nullability
    or an index) triggered a drop-and-recreate of the PRIMARY KEY column itself instead of a
    plain AlterField - which would break every FK referencing it. Mirrors
    BaseSchemaEditor._alter_generated_field()'s own identical pk exclusion."""
    OldWidget = make_model("Widget", "widget", id=fields.IntField(primary_key=True), name=fields.TextField())
    NewWidget = make_model(
        "Widget", "widget", id=fields.IntField(primary_key=True, description="Primary key"), name=fields.TextField()
    )

    old_state = build_state("models", OldWidget)
    new_state = build_state("models", NewWidget)

    operations = OperationGenerator(old_state, new_state).generate()
    assert len(operations) == 1
    assert isinstance(operations[0], AlterField)
    assert operations[0].name == "id"


def test_opclasses_only_index_change_is_detected() -> None:
    """_index_signature() (used to match an old/new index as "the same index" so no operation
    gets generated for it) ignored Index.opclasses entirely - a GinIndex whose fields/type/extra
    were all unchanged but whose opclasses were swapped (a real DDL change - a different operator
    class) matched the old signature and was silently treated as a no-op, so the autodetector
    never generated the RemoveIndex+AddIndex needed to actually apply it."""
    OldWidget = make_model("Widget", "widget", id=fields.IntField(primary_key=True), data=fields.TextField())
    NewWidget = make_model("Widget", "widget", id=fields.IntField(primary_key=True), data=fields.TextField())
    OldWidget._meta.indexes = (GinIndex(fields=("data",), name="widget_data_idx", opclasses=("gin_trgm_ops",)),)
    NewWidget._meta.indexes = (GinIndex(fields=("data",), name="widget_data_idx", opclasses=("gin__int_ops",)),)

    old_state = build_state("models", OldWidget)
    new_state = build_state("models", NewWidget)

    operations = OperationGenerator(old_state, new_state).generate()
    assert any(isinstance(op, RemoveIndex) and op.name == "widget_data_idx" for op in operations)
    assert any(
        isinstance(op, AddIndex) and op.index.name == "widget_data_idx" and op.index.opclasses == ["gin__int_ops"]
        for op in operations
    )


# ---------------------------------------------------------------------------
# RenameField.state_forward()'s Meta-level bookkeeping (pk/unique_together/indexes), as seen by
# a SECOND autodetector run against the projected state a RenameField migration leaves behind.
# ---------------------------------------------------------------------------


def test_no_further_changes_detected_after_renaming_pk_field() -> None:
    """Before the fix, RenameField.state_forward() never updated the projected state's
    pk_field_name/pk_attr - so a rename of the PK field, once applied, made every subsequent
    makemigrations run raise ConfigurationError("Changing the primary key...") forever after,
    even though nothing further had changed."""
    Widget = make_model("Widget", "widget", id=fields.IntField(primary_key=True), name=fields.TextField())
    projected_old_state = build_state("models", Widget)
    RenameField(model_name="Widget", old_name="id", new_name="pk_id").state_forward("models", projected_old_state)

    RenamedWidget = make_model("Widget", "widget", pk_id=fields.IntField(primary_key=True), name=fields.TextField())
    new_state = build_state("models", RenamedWidget)

    operations = OperationGenerator(projected_old_state, new_state).generate()
    assert operations == []


def test_no_spurious_constraint_ops_after_renaming_unique_together_field() -> None:
    """A renamed field that participates in unique_together must not produce a spurious
    RemoveConstraint+AddConstraint pair on the next autodetector run - the projected state's
    unique_together must have followed the rename."""
    Widget = make_model(
        "Widget",
        "widget",
        {"constraints": [UniqueConstraint(fields=("email", "tenant"))]},
        id=fields.IntField(primary_key=True),
        email=fields.TextField(),
        tenant=fields.TextField(),
    )
    projected_old_state = build_state("models", Widget)
    RenameField(model_name="Widget", old_name="email", new_name="address").state_forward("models", projected_old_state)

    RenamedWidget = make_model(
        "Widget",
        "widget",
        {"constraints": [UniqueConstraint(fields=("address", "tenant"))]},
        id=fields.IntField(primary_key=True),
        address=fields.TextField(),
        tenant=fields.TextField(),
    )
    new_state = build_state("models", RenamedWidget)

    operations = OperationGenerator(projected_old_state, new_state).generate()
    assert not any(isinstance(op, (RemoveConstraint, AddConstraint)) for op in operations)
    assert operations == []


def test_no_spurious_index_ops_after_renaming_indexed_field() -> None:
    """A renamed field referenced from a Meta.indexes entry must not produce a spurious
    RemoveIndex+AddIndex pair on the next autodetector run - the projected state's indexes must
    have followed the rename."""
    Widget = make_model(
        "Widget",
        "widget",
        {"indexes": [Index(fields=("sku",), name="idx_sku")]},
        id=fields.IntField(primary_key=True),
        sku=fields.TextField(),
    )
    projected_old_state = build_state("models", Widget)
    RenameField(model_name="Widget", old_name="sku", new_name="code").state_forward("models", projected_old_state)

    RenamedWidget = make_model(
        "Widget",
        "widget",
        {"indexes": [Index(fields=("code",), name="idx_sku")]},
        id=fields.IntField(primary_key=True),
        code=fields.TextField(),
    )
    new_state = build_state("models", RenamedWidget)

    operations = OperationGenerator(projected_old_state, new_state).generate()
    assert not any(isinstance(op, (RemoveIndex, AddIndex)) for op in operations)
    assert operations == []


def test_generate_add_remove_index() -> None:
    OldWidget = make_model("Widget", "widget", id=fields.IntField(primary_key=True), name=fields.TextField())
    NewWidget = make_model(
        "Widget",
        "widget",
        {"indexes": (("name",),)},
        id=fields.IntField(primary_key=True),
        name=fields.TextField(),
    )

    old_state = build_state("models", OldWidget)
    new_state = build_state("models", NewWidget)

    operations = OperationGenerator(old_state, new_state).generate()
    assert any(isinstance(op, AddIndex) for op in operations)

    operations = OperationGenerator(new_state, old_state).generate()
    assert any(isinstance(op, RemoveIndex) for op in operations)


def test_generate_rename_index_explicit() -> None:
    OldWidget = make_model(
        "Widget",
        "widget",
        {"indexes": (Index(fields=("name",), name="idx_old"),)},
        id=fields.IntField(primary_key=True),
        name=fields.TextField(),
    )
    NewWidget = make_model(
        "Widget",
        "widget",
        {"indexes": (Index(fields=("name",), name="idx_new"),)},
        id=fields.IntField(primary_key=True),
        name=fields.TextField(),
    )

    old_state = build_state("models", OldWidget)
    new_state = build_state("models", NewWidget)

    operations = OperationGenerator(old_state, new_state).generate()
    assert any(isinstance(op, RenameIndex) for op in operations)


def test_generate_expression_index_does_not_crash_diff() -> None:
    """An expression-based Index has no field_names until resolved against a live model -
    state diffing only ever has a ModelState snapshot, never one. Diffing a model that has an
    expression index, alongside some other real change, must not raise
    ConfigurationError("Index expressions must be resolved...") - it must still produce
    operations for the unrelated change."""
    expr_index = Index(F("name"), name="idx_widget_name_expr")
    OldWidget = make_model(
        "Widget",
        "widget",
        {"indexes": (expr_index,)},
        id=fields.IntField(primary_key=True),
        name=fields.TextField(),
    )
    NewWidget = make_model(
        "Widget",
        "widget",
        {"indexes": (expr_index,)},
        id=fields.IntField(primary_key=True),
        name=fields.TextField(),
        extra=fields.TextField(null=True),
    )

    old_state = build_state("models", OldWidget)
    new_state = build_state("models", NewWidget)

    operations = OperationGenerator(old_state, new_state).generate()
    assert any(isinstance(op, AddField) and op.name == "extra" for op in operations)


def test_generate_unique_together_constraints() -> None:
    OldWidget = make_model(
        "Widget",
        "widget",
        {"constraints": [UniqueConstraint(fields=("name", "age"))]},
        id=fields.IntField(primary_key=True),
        name=fields.TextField(),
        age=fields.IntField(),
    )
    NewWidget = make_model(
        "Widget",
        "widget",
        {"constraints": [UniqueConstraint(fields=("name",))]},
        id=fields.IntField(primary_key=True),
        name=fields.TextField(),
        age=fields.IntField(),
    )

    old_state = build_state("models", OldWidget)
    new_state = build_state("models", NewWidget)

    operations = OperationGenerator(old_state, new_state).generate()
    assert any(isinstance(op, RemoveConstraint) for op in operations)
    assert any(isinstance(op, AddConstraint) for op in operations)


def test_generate_swap_unique_together_to_unique_index_adds_before_it_removes() -> None:
    """unique_together -> Index(unique=True) on the same fields is a pure representation swap -
    Postgres has no in-place conversion between a UNIQUE CONSTRAINT and a UNIQUE INDEX, so this
    generates a RemoveConstraint for the old representation and an AddIndex for the new one. The
    generic "adds always last" ordering rule would run the RemoveConstraint before the AddIndex,
    opening a real gap (on a non-atomic migration) where (a, b) has no uniqueness enforcement at
    all - the add must run first so the two always overlap instead."""
    OldWidget = make_model(
        "Widget",
        "widget",
        {"constraints": [UniqueConstraint(fields=("a", "b"))]},
        id=fields.IntField(primary_key=True),
        a=fields.TextField(),
        b=fields.TextField(),
    )
    NewWidget = make_model(
        "Widget",
        "widget",
        {"indexes": [Index(fields=("a", "b"), unique=True)]},
        id=fields.IntField(primary_key=True),
        a=fields.TextField(),
        b=fields.TextField(),
    )

    old_state = build_state("models", OldWidget)
    new_state = build_state("models", NewWidget)

    operations = OperationGenerator(old_state, new_state).generate()
    add_index_positions = [i for i, op in enumerate(operations) if isinstance(op, AddIndex)]
    remove_constraint_positions = [i for i, op in enumerate(operations) if isinstance(op, RemoveConstraint)]
    assert len(add_index_positions) == 1
    assert len(remove_constraint_positions) == 1
    assert add_index_positions[0] < remove_constraint_positions[0]


def test_generate_swap_unique_index_to_unique_together_adds_before_it_removes() -> None:
    """The reverse direction of the swap above."""
    OldWidget = make_model(
        "Widget",
        "widget",
        {"indexes": [Index(fields=("a", "b"), unique=True)]},
        id=fields.IntField(primary_key=True),
        a=fields.TextField(),
        b=fields.TextField(),
    )
    NewWidget = make_model(
        "Widget",
        "widget",
        {"constraints": [UniqueConstraint(fields=("a", "b"))]},
        id=fields.IntField(primary_key=True),
        a=fields.TextField(),
        b=fields.TextField(),
    )

    old_state = build_state("models", OldWidget)
    new_state = build_state("models", NewWidget)

    operations = OperationGenerator(old_state, new_state).generate()
    add_constraint_positions = [i for i, op in enumerate(operations) if isinstance(op, AddConstraint)]
    remove_index_positions = [i for i, op in enumerate(operations) if isinstance(op, RemoveIndex)]
    assert len(add_constraint_positions) == 1
    assert len(remove_index_positions) == 1
    assert add_constraint_positions[0] < remove_index_positions[0]


def test_generate_rename_constraint_explicit() -> None:
    Widget = make_model("Widget", "widget", id=fields.IntField(primary_key=True), name=fields.TextField())

    old_state = build_state("models", Widget)
    new_state = build_state("models", Widget)

    old_state.models[("models", "Widget")].options["constraints"] = (
        UniqueConstraint(fields=("name",), name="uq_old"),
    )
    new_state.models[("models", "Widget")].options["constraints"] = (
        UniqueConstraint(fields=("name",), name="uq_new"),
    )

    operations = OperationGenerator(old_state, new_state).generate()
    assert any(isinstance(op, RenameConstraint) for op in operations)


def test_generate_remove_unnamed_uniqueconstraint_from_meta_constraints() -> None:
    """A UniqueConstraint declared via Meta.constraints (not unique_together) can have no
    explicit name= at all - unlike CheckConstraint/ExclusionConstraint, which always require
    one. RemoveConstraint requires name OR fields, so emitting name=constraint.name (None here)
    raised ValueError outright, blocking makemigrations from generating a migration that drops
    such a constraint at all."""
    Widget = make_model(
        "Order", "order", id=fields.IntField(primary_key=True), resource=fields.IntField(), slug=fields.TextField()
    )

    old_state = build_state("models", Widget)
    new_state = build_state("models", Widget)
    old_state.models[("models", "Order")].options["constraints"] = (UniqueConstraint(fields=("resource", "slug")),)
    new_state.models[("models", "Order")].options["constraints"] = ()

    operations = OperationGenerator(old_state, new_state).generate()
    (remove_op,) = [op for op in operations if isinstance(op, RemoveConstraint)]
    assert remove_op.name is None
    assert remove_op.fields == ["resource", "slug"]


def test_detect_db_default_added() -> None:
    OldWidget = make_model("Widget", "widget", id=fields.IntField(primary_key=True), value=fields.IntField())
    NewWidget = make_model(
        "Widget", "widget", id=fields.IntField(primary_key=True), value=fields.IntField(db_default=42)
    )

    old_state = build_state("models", OldWidget)
    new_state = build_state("models", NewWidget)

    operations = OperationGenerator(old_state, new_state).generate()
    assert len(operations) == 1
    assert isinstance(operations[0], AlterField)
    assert operations[0].name == "value"


def test_detect_db_default_changed() -> None:
    OldWidget = make_model(
        "Widget", "widget", id=fields.IntField(primary_key=True), value=fields.IntField(db_default=42)
    )
    NewWidget = make_model(
        "Widget", "widget", id=fields.IntField(primary_key=True), value=fields.IntField(db_default=100)
    )

    old_state = build_state("models", OldWidget)
    new_state = build_state("models", NewWidget)

    operations = OperationGenerator(old_state, new_state).generate()
    assert len(operations) == 1
    assert isinstance(operations[0], AlterField)
    assert operations[0].name == "value"


def test_detect_db_default_removed() -> None:
    OldWidget = make_model(
        "Widget", "widget", id=fields.IntField(primary_key=True), value=fields.IntField(db_default=42)
    )
    NewWidget = make_model("Widget", "widget", id=fields.IntField(primary_key=True), value=fields.IntField())

    old_state = build_state("models", OldWidget)
    new_state = build_state("models", NewWidget)

    operations = OperationGenerator(old_state, new_state).generate()
    assert len(operations) == 1
    assert isinstance(operations[0], AlterField)
    assert operations[0].name == "value"


def test_detect_check_constraint_added() -> None:
    Widget = make_model("Widget", "widget", id=fields.IntField(primary_key=True), price=fields.IntField())

    old_state = build_state("models", Widget)
    new_state = build_state("models", Widget)

    new_state.models[("models", "Widget")].options["constraints"] = (
        CheckConstraint(check=RawSQLTerm("price > 0"), name="ck_price"),
    )

    operations = OperationGenerator(old_state, new_state).generate()
    assert len(operations) == 1
    assert isinstance(operations[0], AddConstraint)
    assert isinstance(operations[0].constraint, CheckConstraint)
    assert operations[0].constraint.name == "ck_price"


def test_detect_check_constraint_removed() -> None:
    Widget = make_model("Widget", "widget", id=fields.IntField(primary_key=True), price=fields.IntField())

    old_state = build_state("models", Widget)
    new_state = build_state("models", Widget)

    old_state.models[("models", "Widget")].options["constraints"] = (
        CheckConstraint(check=RawSQLTerm("price > 0"), name="ck_price"),
    )

    operations = OperationGenerator(old_state, new_state).generate()
    assert len(operations) == 1
    assert isinstance(operations[0], RemoveConstraint)
    assert operations[0].name == "ck_price"


def test_detect_exclusion_constraint_added() -> None:
    Item = make_model("Item", "item", id=fields.IntField(primary_key=True), resource=fields.IntField())

    old_state = build_state("models", Item)
    new_state = build_state("models", Item)

    new_state.models[("models", "Item")].options["constraints"] = (
        ExclusionConstraint(name="no_overlap", expressions=(("resource", "="), ("during", "&&"))),
    )

    operations = OperationGenerator(old_state, new_state).generate()
    # A GiST exclusion over the scalar "resource" column needs btree_gist, created first.
    assert len(operations) == 2
    assert isinstance(operations[0], CreateExtension)
    assert operations[0].extension_name == "btree_gist"
    assert isinstance(operations[1], AddConstraint)
    assert isinstance(operations[1].constraint, ExclusionConstraint)
    assert operations[1].constraint.name == "no_overlap"


def test_detect_exclusion_constraint_removed() -> None:
    Item = make_model("Item", "item", id=fields.IntField(primary_key=True), resource=fields.IntField())

    old_state = build_state("models", Item)
    new_state = build_state("models", Item)

    old_state.models[("models", "Item")].options["constraints"] = (
        ExclusionConstraint(name="no_overlap", expressions=(("resource", "="), ("during", "&&"))),
    )

    operations = OperationGenerator(old_state, new_state).generate()
    # btree_gist, needed only by this constraint, is dropped after it.
    assert len(operations) == 2
    assert isinstance(operations[0], RemoveConstraint)
    assert operations[0].name == "no_overlap"
    assert isinstance(operations[1], RemoveExtension)
    assert operations[1].extension_name == "btree_gist"


def test_detect_exclusion_constraint_renamed() -> None:
    """Same expressions/using/condition, only the name differs - a clean rename, not a
    remove+add, mirroring CheckConstraint's own check-text-keyed rename detection."""
    Item = make_model("Item", "item", id=fields.IntField(primary_key=True), resource=fields.IntField())

    old_state = build_state("models", Item)
    new_state = build_state("models", Item)

    old_state.models[("models", "Item")].options["constraints"] = (
        ExclusionConstraint(name="no_overlap_old", expressions=(("resource", "="), ("during", "&&"))),
    )
    new_state.models[("models", "Item")].options["constraints"] = (
        ExclusionConstraint(name="no_overlap_new", expressions=(("resource", "="), ("during", "&&"))),
    )

    operations = OperationGenerator(old_state, new_state).generate()
    assert len(operations) == 1
    assert isinstance(operations[0], RenameConstraint)
    assert operations[0].old_name == "no_overlap_old"
    assert operations[0].new_name == "no_overlap_new"


def test_detect_trigger_added() -> None:
    Widget = make_model("Widget", "widget", id=fields.IntField(primary_key=True), price=fields.IntField())

    old_state = build_state("models", Widget)
    new_state = build_state("models", Widget)

    new_state.models[("models", "Widget")].options["triggers"] = (
        Trigger(name="widget_trig", on=TriggerEvent.INSERT, body="RETURN NEW;"),
    )

    operations = OperationGenerator(old_state, new_state).generate()
    assert len(operations) == 1
    assert isinstance(operations[0], AddTrigger)
    assert operations[0].trigger.name == "widget_trig"


def test_detect_trigger_removed() -> None:
    Widget = make_model("Widget", "widget", id=fields.IntField(primary_key=True), price=fields.IntField())

    old_state = build_state("models", Widget)
    new_state = build_state("models", Widget)

    old_state.models[("models", "Widget")].options["triggers"] = (
        Trigger(name="widget_trig", on=TriggerEvent.INSERT, body="RETURN NEW;"),
    )

    operations = OperationGenerator(old_state, new_state).generate()
    assert len(operations) == 1
    assert isinstance(operations[0], RemoveTrigger)
    assert operations[0].name == "widget_trig"


def test_detect_trigger_altered() -> None:
    Widget = make_model("Widget", "widget", id=fields.IntField(primary_key=True), price=fields.IntField())

    old_state = build_state("models", Widget)
    new_state = build_state("models", Widget)

    old_state.models[("models", "Widget")].options["triggers"] = (
        Trigger(name="widget_trig", on=TriggerEvent.INSERT, body="RETURN NEW;"),
    )
    new_state.models[("models", "Widget")].options["triggers"] = (
        Trigger(name="widget_trig", on="INSERT OR UPDATE", body="RETURN NEW;"),
    )

    operations = OperationGenerator(old_state, new_state).generate()
    assert len(operations) == 1
    assert isinstance(operations[0], AlterTrigger)
    assert operations[0].trigger.on == "INSERT OR UPDATE"


def test_trigger_unchanged_produces_no_operations() -> None:
    Widget = make_model("Widget", "widget", id=fields.IntField(primary_key=True), price=fields.IntField())

    old_state = build_state("models", Widget)
    new_state = build_state("models", Widget)

    trigger = Trigger(name="widget_trig", on=TriggerEvent.INSERT, body="RETURN NEW;")
    old_state.models[("models", "Widget")].options["triggers"] = (trigger,)
    new_state.models[("models", "Widget")].options["triggers"] = (trigger,)

    operations = OperationGenerator(old_state, new_state).generate()
    assert operations == []


def test_detect_trigger_renamed() -> None:
    """Same definition, different name -> a single RenameTrigger, not RemoveTrigger+AddTrigger -
    mirrors the rename detection CheckConstraint already gets via its check-text keying."""
    Widget = make_model("Widget", "widget", id=fields.IntField(primary_key=True), price=fields.IntField())

    old_state = build_state("models", Widget)
    new_state = build_state("models", Widget)

    old_state.models[("models", "Widget")].options["triggers"] = (
        Trigger(name="widget_trig_old", on=TriggerEvent.INSERT, body="RETURN NEW;"),
    )
    new_state.models[("models", "Widget")].options["triggers"] = (
        Trigger(name="widget_trig_new", on=TriggerEvent.INSERT, body="RETURN NEW;"),
    )

    operations = OperationGenerator(old_state, new_state).generate()
    assert len(operations) == 1
    assert isinstance(operations[0], RenameTrigger)
    assert operations[0].old_name == "widget_trig_old"
    assert operations[0].new_name == "widget_trig_new"


def test_generate_alter_trigger_and_add_field_in_correct_order() -> None:
    """A trigger whose new body references a column being added in the same migration must run
    AFTER AddField - AlterTrigger needs the same always_last treatment AddTrigger already gets."""
    OldWidget = make_model(
        "Widget",
        "widget",
        {"triggers": [Trigger(name="widget_trig", on=TriggerEvent.INSERT, body="RETURN NEW;")]},
        id=fields.IntField(primary_key=True),
    )
    NewWidget = make_model(
        "Widget",
        "widget",
        {"triggers": [Trigger(name="widget_trig", on=TriggerEvent.INSERT, body="NEW.value := 1; RETURN NEW;")]},
        id=fields.IntField(primary_key=True),
        value=fields.IntField(default=0),
    )

    old_state = build_state("models", OldWidget)
    new_state = build_state("models", NewWidget)

    operations = OperationGenerator(old_state, new_state).generate()
    operation_types = [type(op).__name__ for op in operations]
    assert operation_types.index("AddField") < operation_types.index("AlterTrigger")


def test_generate_alter_field_and_add_index_in_correct_order() -> None:
    OldWidget = make_model("Widget", "widget", id=fields.IntField(primary_key=True))
    NewWidget = make_model(
        "Widget",
        "widget",
        {"indexes": [Index(fields=["value"])]},
        id=fields.IntField(primary_key=True),
        value=fields.IntField(),
    )

    old_state = build_state("models", OldWidget)
    new_state = build_state("models", NewWidget)

    operations = OperationGenerator(old_state, new_state).generate()
    assert len(operations) == 2
    assert isinstance(operations[0], AddField)
    assert isinstance(operations[1], AddIndex)


def test_generate_alter_field_and_add_constraint_in_correct_order() -> None:
    OldWidget = make_model("Widget", "widget", id=fields.IntField(primary_key=True))
    NewWidget = make_model(
        "Widget",
        "widget",
        {"constraints": [UniqueConstraint(fields=("value",), name="uq_new")]},
        id=fields.IntField(primary_key=True),
        value=fields.IntField(),
    )

    old_state = build_state("models", OldWidget)
    new_state = build_state("models", NewWidget)

    operations = OperationGenerator(old_state, new_state).generate()
    assert len(operations) == 2
    assert isinstance(operations[0], AddField)
    assert isinstance(operations[1], AddConstraint)


def test_generate_alter_field_and_remove_index_in_correct_order() -> None:
    OldWidget = make_model(
        "Widget",
        "widget",
        {"indexes": [Index(fields=["value"])]},
        id=fields.IntField(primary_key=True),
        value=fields.IntField(),
    )
    NewWidget = make_model("Widget", "widget", id=fields.IntField(primary_key=True))

    old_state = build_state("models", OldWidget)
    new_state = build_state("models", NewWidget)

    operations = OperationGenerator(old_state, new_state).generate()
    assert len(operations) == 2
    assert isinstance(operations[0], RemoveIndex)
    assert isinstance(operations[1], RemoveField)


def test_generate_alter_field_and_remove_constraint_in_correct_order() -> None:
    OldWidget = make_model(
        "Widget",
        "widget",
        {"constraints": [UniqueConstraint(fields=("value",), name="uq_new")]},
        id=fields.IntField(primary_key=True),
        value=fields.IntField(),
    )
    NewWidget = make_model("Widget", "widget", id=fields.IntField(primary_key=True))

    old_state = build_state("models", OldWidget)
    new_state = build_state("models", NewWidget)

    operations = OperationGenerator(old_state, new_state).generate()
    assert len(operations) == 2
    assert isinstance(operations[0], RemoveConstraint)
    assert isinstance(operations[1], RemoveField)


def test_generate_delete_model_reverse_dependency_order() -> None:
    """DeleteModel operations for two related models removed in the same autodetect round must
    be emitted dependent-first (reverse of CreateModel's dependencies-first order) - Order (which
    FKs Customer) must be deleted before Customer, or DeleteModel.state_forward() raises
    IncompatibleStateError for the still-referenced Customer. CreateModel already gets this right
    via _sort_by_dependencies(); this is DeleteModel's own (previously missing) mirror of it."""
    Customer = make_model("Customer", "customer", id=fields.IntField(primary_key=True))
    Order = make_model(
        "Order",
        "order",
        id=fields.IntField(primary_key=True),
        customer=fields.ForeignKeyField("models.Customer", related_name="orders"),
    )

    old_state = build_state("models", Customer, Order)
    new_state = build_state("models")

    operations = OperationGenerator(old_state, new_state).generate()
    delete_ops = [op for op in operations if isinstance(op, DeleteModel)]
    assert [op.name for op in delete_ops] == ["Order", "Customer"]


def test_generate_create_model_mutual_fk_cycle_defers_one_field() -> None:
    """Two brand-new models with a genuine mutual FK cycle (Team.captain -> Player,
    Player.team -> Team) can't be topologically ordered - CreateModel's inline
    `... REFERENCES ...` always needs the target table to already exist. One field must be
    deferred: created without it, then added back via AddField once both models exist. Applying
    the generated operations' own state_forward() in order (the way the real executor replays
    them) must not raise IncompatibleStateError."""
    Team = make_model(
        "Team",
        "team",
        id=fields.IntField(primary_key=True),
        captain=fields.ForeignKeyField("models.Player", related_name="captained_teams", null=True),
    )
    Player = make_model(
        "Player",
        "player",
        id=fields.IntField(primary_key=True),
        team=fields.ForeignKeyField("models.Team", related_name="players", null=True),
    )

    old_state = build_state("models")
    new_state = build_state("models", Team, Player)

    operations = OperationGenerator(old_state, new_state).generate()
    create_ops = [op for op in operations if isinstance(op, CreateModel)]
    add_field_ops = [op for op in operations if isinstance(op, AddField)]
    assert {op.name for op in create_ops} == {"Team", "Player"}
    # Exactly one of the two mutual FKs was deferred out of CreateModel and added back after.
    assert len(add_field_ops) == 1
    assert add_field_ops[0].name in ("captain", "team")
    # The deferred AddField must come after BOTH CreateModel operations - its target model must
    # already exist for the FK to be valid.
    assert operations.index(add_field_ops[0]) > max(operations.index(op) for op in create_ops)

    tracked_state = State(models={}, apps=StateApps())
    for op in operations:
        op.state_forward("models", tracked_state)
    assert set(tracked_state.models) == {("models", "Team"), ("models", "Player")}


def test_generate_delete_model_mutual_fk_cycle_defers_one_field() -> None:
    """Mirror of test_generate_create_model_mutual_fk_cycle_defers_one_field for deletion: two
    EXISTING models with a genuine mutual FK cycle, both removed in the same autodetect round,
    reproduce the identical cycle on the way out - DeleteModel.state_forward() would otherwise
    always raise IncompatibleStateError (the still-referenced model, regardless of delete order)
    for whichever of the two models is dropped second. One field must be removed via RemoveField
    BEFORE either DeleteModel runs, breaking the cycle the same way CreateModel's own
    exclude_fields=+AddField-after breaks it on the way in."""
    Team = make_model(
        "Team",
        "team",
        id=fields.IntField(primary_key=True),
        captain=fields.ForeignKeyField("models.Player", related_name="captained_teams", null=True),
    )
    Player = make_model(
        "Player",
        "player",
        id=fields.IntField(primary_key=True),
        team=fields.ForeignKeyField("models.Team", related_name="players", null=True),
    )

    old_state = build_state("models", Team, Player)
    new_state = build_state("models")

    operations = OperationGenerator(old_state, new_state).generate()
    remove_field_ops = [op for op in operations if isinstance(op, RemoveField)]
    delete_ops = [op for op in operations if isinstance(op, DeleteModel)]
    assert {op.name for op in delete_ops} == {"Team", "Player"}
    assert len(remove_field_ops) == 1
    assert remove_field_ops[0].name in ("captain", "team")
    # The RemoveField must come before BOTH DeleteModel operations - neither model may still
    # carry a live FK to the other when it's dropped.
    assert operations.index(remove_field_ops[0]) < min(operations.index(op) for op in delete_ops)

    # Replaying state_forward() in generated order (what the real executor does) must not raise
    # IncompatibleStateError - this is exactly what used to fail before the fix, regardless of
    # DeleteModel order, since removing either model first still left the other one's FK
    # dangling.
    tracked_state = build_state("models", Team, Player)
    for op in operations:
        op.state_forward("models", tracked_state)
    assert tracked_state.models == {}


def test_removing_a_named_expression_index_generates_remove_index_by_name() -> None:
    """An expression-based index has no column list to identify it by, so RemoveIndex(fields=...)
    is impossible for it - detect_drift() carries the live database's real index name onto the
    observed index instead, and an observed expression index the models no longer declare must
    come out as RemoveIndex(name=...) rather than crashing on "RemoveIndex requires name or
    fields"."""
    from hare.ddl.raw_sql_term import RawSQLTerm

    OldWidget = make_model("Widget", "widget", id=fields.IntField(primary_key=True), name=fields.TextField())
    NewWidget = make_model("Widget", "widget", id=fields.IntField(primary_key=True), name=fields.TextField())
    OldWidget._meta.indexes = (Index(RawSQLTerm("lower(name)"), name="widget_lower_name_idx"),)

    operations = OperationGenerator(build_state("models", OldWidget), build_state("models", NewWidget)).generate()

    assert any(isinstance(op, RemoveIndex) and op.name == "widget_lower_name_idx" for op in operations)


def test_detect_unique_constraint_renamed_with_changed_options_as_remove_and_add() -> None:
    """A UniqueConstraint renamed and made deferrable in one step was taken for a plain rename -
    the migration renamed it and never applied the new DEFERRABLE clause."""
    Item = make_model("Item", "item", id=fields.IntField(primary_key=True), a=fields.IntField(), b=fields.IntField())

    old_state = build_state("models", Item)
    new_state = build_state("models", Item)
    old_state.models[("models", "Item")].options["constraints"] = (
        UniqueConstraint(fields=("a", "b"), name="uq_item_old"),
    )
    new_state.models[("models", "Item")].options["constraints"] = (
        UniqueConstraint(fields=("a", "b"), name="uq_item_new", deferrable=True),
    )

    operations = OperationGenerator(old_state, new_state).generate()

    assert [type(operation).__name__ for operation in operations] == ["RemoveConstraint", "AddConstraint"]
    assert operations[0].name == "uq_item_old"
    assert operations[1].constraint == UniqueConstraint(fields=("a", "b"), name="uq_item_new", deferrable=True)


def test_detect_deferrable_unique_constraint_renamed_keeps_its_options() -> None:
    Item = make_model("Item", "item", id=fields.IntField(primary_key=True), a=fields.IntField(), b=fields.IntField())

    old_state = build_state("models", Item)
    new_state = build_state("models", Item)
    old_state.models[("models", "Item")].options["constraints"] = (
        UniqueConstraint(fields=("a", "b"), name="uq_item_old", deferrable=True, initially_deferred=True),
    )
    new_constraint = UniqueConstraint(fields=("a", "b"), name="uq_item_new", deferrable=True, initially_deferred=True)
    new_state.models[("models", "Item")].options["constraints"] = (new_constraint,)

    (operation,) = OperationGenerator(old_state, new_state).generate()
    assert isinstance(operation, RenameConstraint)
    operation.state_forward("models", old_state)

    assert tuple(old_state.models[("models", "Item")].options["constraints"]) == (new_constraint,)
