from __future__ import annotations

from typing import Any, cast

import pytest

from hare import fields
from hare.ddl.constraints import UniqueConstraint
from hare.ddl.indexes import Index
from hare.fields.relations.fields import (
    BackwardForeignKeyRelation,
    BackwardOneToOneRelation,
    ForeignKeyFieldInstance,
    ManyToManyFieldInstance,
    OneToOneFieldInstance,
    RelationalField,
)
from hare.migrations.exceptions import IncompatibleStateError
from hare.migrations.operations import (
    AddField,
    AlterField,
    AlterModelOptions,
    CreateModel,
    DeleteModel,
    RemoveField,
    RenameField,
    RenameModel,
)
from hare.migrations.state.state import State

RelationalFieldInstance = ForeignKeyFieldInstance[Any] | OneToOneFieldInstance[Any] | ManyToManyFieldInstance[Any]


def test_add_model_only_id(empty_state: State):
    state = empty_state
    operation = CreateModel(name="TestModel", fields=[("id", fields.IntField(primary_key=True))])

    operation.state_forward("models", state)
    assert len(state.models) == 1
    assert len(state.apps.apps) == 1
    assert len(state.apps.apps["models"]) == 1

    model = state.apps.get_model("models.TestModel")
    assert isinstance(model._meta.pk, fields.IntField)


def test_add_model_simple_fields(empty_state: State):
    state = empty_state
    operation = CreateModel(
        name="TestModel",
        fields=[("id", fields.IntField(primary_key=True)), ("name", fields.TextField())],
    )
    operation.state_forward("models", state)

    model = state.apps.get_model("models.TestModel")
    assert len(model._meta.fields) == 2
    field = model._meta.fields_map["name"]
    assert isinstance(field, fields.TextField)
    model_state = state.models[("models", "TestModel")]

    assert len(model_state.fields) == 2


def test_add_model_two_simple_models_fields_in_one_app(empty_state: State):
    state = empty_state
    operation = CreateModel(
        name="TestModel",
        fields=[("id", fields.IntField(primary_key=True)), ("name", fields.TextField())],
    )
    operation.state_forward("models", state)

    operation = CreateModel(
        name="TestModel2",
        fields=[("id", fields.IntField(primary_key=True)), ("counter", fields.IntField())],
    )
    operation.state_forward("models", state)

    assert len(state.models) == 2
    assert len(state.apps.apps) == 1
    assert len(state.apps.apps["models"]) == 2


def test_add_model_two_simple_models_fields_in_two_apps(empty_state: State):
    state = empty_state
    operation = CreateModel(
        name="TestModel",
        fields=[("id", fields.IntField(primary_key=True)), ("name", fields.TextField())],
    )
    operation.state_forward("models", state)

    operation = CreateModel(
        name="TestModel2",
        fields=[("id", fields.IntField(primary_key=True)), ("counter", fields.IntField())],
    )
    operation.state_forward("models2", state)

    assert len(state.models) == 2
    assert len(state.apps.apps) == 2
    assert len(state.apps.apps["models"]) == 1
    assert len(state.apps.apps["models2"]) == 1


@pytest.mark.parametrize(
    ["field_class", "second_app", "models_in_second_app"],
    [
        (ForeignKeyFieldInstance, "models", 2),
        (ForeignKeyFieldInstance, "models2", 1),
        (OneToOneFieldInstance, "models", 2),
        (OneToOneFieldInstance, "models2", 1),
        (ManyToManyFieldInstance, "models", 2),
        (ManyToManyFieldInstance, "models2", 1),
    ],
)
def test_add_model_two_simple_models_fields_in_one_app_with_fk(
    empty_state: State,
    field_class: type[ForeignKeyFieldInstance | OneToOneFieldInstance | ManyToManyFieldInstance],
    second_app: str,
    models_in_second_app: int,
):
    state = empty_state
    operation = CreateModel(
        name="TestModel",
        fields=[("id", fields.IntField(primary_key=True)), ("name", fields.TextField())],
    )
    operation.state_forward("models", state)

    operation = CreateModel(
        name="TestModel2",
        fields=[
            ("id", fields.IntField(primary_key=True)),
            ("reference", field_class("models.TestModel", related_name="children")),
        ],
    )
    operation.state_forward(second_app, state)

    assert len(state.models) == 2
    assert len(state.apps.apps[second_app]) == models_in_second_app

    model2 = state.apps.get_model(f"{second_app}.TestModel2")
    fk_field = cast(RelationalFieldInstance, model2._meta.fields_map["reference"])
    assert isinstance(fk_field, field_class)
    assert fk_field.related_model.__name__ == "TestModel"


@pytest.mark.parametrize(
    "field_class",
    [ForeignKeyFieldInstance, OneToOneFieldInstance, ManyToManyFieldInstance],
)
def test_create_model_with_fk_to_not_yet_created_model(
    empty_state: State,
    field_class: type[ForeignKeyFieldInstance | OneToOneFieldInstance | ManyToManyFieldInstance],
):
    """Regression test: CreateModel should not raise when the FK target model
    hasn't been added to state yet (simulates alphabetical ordering where
    e.g. 'Event' is created before 'Tournament')."""
    state = empty_state

    # Create the model that references another model not yet in state.
    # This simulates alphabetical ordering: Alert (with FK to Warehouse) before Warehouse.
    op1 = CreateModel(
        name="Alert",
        fields=[
            ("id", fields.IntField(primary_key=True)),
            ("reference", field_class("models.Warehouse", related_name="alerts")),
        ],
    )
    op1.state_forward("models", state)

    # Now create the referenced model.
    op2 = CreateModel(
        name="Warehouse",
        fields=[
            ("id", fields.IntField(primary_key=True)),
            ("name", fields.TextField()),
        ],
    )
    op2.state_forward("models", state)

    assert len(state.models) == 2

    model_alert = state.apps.get_model("models.Alert")
    fk_field = cast(RelationalFieldInstance, model_alert._meta.fields_map["reference"])
    assert isinstance(fk_field, field_class)
    assert fk_field.related_model.__name__ == "Warehouse"

    # Validation should pass — all relations are resolved
    state.validate_relations_initialized()


def test_validate_catches_permanently_unresolved_relation(empty_state: State):
    """validate_relations_initialized should raise when a FK target model
    is never created, ensuring silent corruption cannot happen."""
    state = empty_state

    op = CreateModel(
        name="Orphan",
        fields=[
            ("id", fields.IntField(primary_key=True)),
            ("ref", ForeignKeyFieldInstance("models.Ghost", related_name="orphans")),
        ],
    )
    op.state_forward("models", state)

    with pytest.raises(RuntimeError, match="uninitialized relations"):
        state.validate_relations_initialized()


def test_simple_rename(state_with_model: State):
    operation = RenameModel("TestModel", "NewName")
    operation.state_forward("models", state_with_model)

    model_state = state_with_model.models[("models", "NewName")]
    assert model_state.name == "NewName"

    model = state_with_model.apps.get_model("models.NewName")
    assert model.__name__ == "NewName"


@pytest.mark.parametrize(
    ["field_class", "second_app"],
    [
        (ForeignKeyFieldInstance, "models"),
        (ForeignKeyFieldInstance, "models2"),
        (OneToOneFieldInstance, "models"),
        (OneToOneFieldInstance, "models2"),
        (ManyToManyFieldInstance, "models"),
        (ManyToManyFieldInstance, "models2"),
    ],
)
def test_rename_with_fk(
    state_with_model: State,
    field_class: type[ForeignKeyFieldInstance | OneToOneFieldInstance | ManyToManyFieldInstance],
    second_app: str,
):
    state = state_with_model
    operation = CreateModel(
        name="TestModel2",
        fields=[
            ("id", fields.IntField(primary_key=True)),
            ("reference", field_class("models.TestModel", related_name="children")),
        ],
    )
    operation.state_forward(second_app, state)

    operation = RenameModel("TestModel", "NewName")
    operation.state_forward("models", state)

    model_state = state.models[(second_app, "TestModel2")]
    field = cast(RelationalFieldInstance, model_state.fields["reference"])
    assert isinstance(field, field_class)
    assert field.model_name == "models.NewName"


def test_simple_delete_model(state_with_model: State):
    operation = DeleteModel("TestModel")
    operation.state_forward("models", state_with_model)

    assert ("models", "TestModel") not in state_with_model.models
    with pytest.raises(KeyError):
        state_with_model.apps.get_model("models.TestModel")


def test_delete_model_fail_on_refs(state_with_model: State):
    state = state_with_model
    operation = CreateModel(
        name="TestModel2",
        fields=[
            ("id", fields.IntField(primary_key=True)),
            (
                "reference",
                ForeignKeyFieldInstance("models.TestModel", related_name="children"),
            ),
        ],
    )
    operation.state_forward("models", state)

    operation = DeleteModel("TestModel")
    with pytest.raises(IncompatibleStateError):
        operation.state_forward("models", state_with_model)


def test_delete_model_with_fk(state_with_model: State):
    state = state_with_model
    CreateModel(
        name="TestModel2",
        fields=[
            ("id", fields.IntField(primary_key=True)),
            ("reference", ForeignKeyFieldInstance("models.TestModel", related_name="children")),
        ],
    ).state_forward("models", state)

    DeleteModel("TestModel2").state_forward("models", state)

    assert ("models", "TestModel2") not in state.models
    with pytest.raises(KeyError):
        state.apps.get_model("models.TestModel2")
    # Target model still intact
    state.apps.get_model("models.TestModel")


def test_delete_model_with_self_reference(state_with_model: State):
    """DeleteModel.state_forward's "is this model still referenced by something else" scan used
    to run over state.models BEFORE excluding the model being deleted from that scan - so a
    self-referencing relation field (e.g. an org-chart "manager" FK back onto the same model)
    made the operation believe the model referenced itself and refused to ever delete it."""
    state = state_with_model
    CreateModel(
        name="TestModel2",
        fields=[
            ("id", fields.IntField(primary_key=True)),
            ("manager", ForeignKeyFieldInstance("models.TestModel2", related_name="reports", null=True)),
        ],
    ).state_forward("models", state)

    DeleteModel("TestModel2").state_forward("models", state)

    assert ("models", "TestModel2") not in state.models
    with pytest.raises(KeyError):
        state.apps.get_model("models.TestModel2")
    # Target of the (now-deleted) model's own self-reference is itself - nothing else to check.


def test_delete_model_with_o2o(state_with_model: State):
    state = state_with_model
    CreateModel(
        name="TestModel2",
        fields=[
            ("id", fields.IntField(primary_key=True)),
            ("reference", OneToOneFieldInstance("models.TestModel", related_name="o2o_rel")),
        ],
    ).state_forward("models", state)

    DeleteModel("TestModel2").state_forward("models", state)

    assert ("models", "TestModel2") not in state.models
    with pytest.raises(KeyError):
        state.apps.get_model("models.TestModel2")
    state.apps.get_model("models.TestModel")


def test_delete_model_with_m2m(state_with_model: State):
    state = state_with_model
    CreateModel(
        name="TestModel2",
        fields=[
            ("id", fields.IntField(primary_key=True)),
            ("reference", ManyToManyFieldInstance("models.TestModel", related_name="m2m_rel")),
        ],
    ).state_forward("models", state)

    DeleteModel("TestModel2").state_forward("models", state)

    assert ("models", "TestModel2") not in state.models
    with pytest.raises(KeyError):
        state.apps.get_model("models.TestModel2")
    state.apps.get_model("models.TestModel")


def test_create_model_with_m2m_through_model(empty_state: State):
    """A ManyToManyField(through="app.Model") declared BEFORE the through model itself is
    created (Person's CreateModel runs before Membership's) - StateApps must defer resolving
    Person's relation until Membership is registered, the same deferral mechanism already used
    for a plain FK/O2O/M2M `model_name` reference to a not-yet-created model."""
    state = empty_state
    CreateModel(
        name="Person",
        fields=[
            ("id", fields.IntField(primary_key=True)),
            (
                "groups",
                ManyToManyFieldInstance("models.Group", through="models.Membership", related_name="members"),
            ),
        ],
    ).state_forward("models", state)
    CreateModel(name="Group", fields=[("id", fields.IntField(primary_key=True))]).state_forward("models", state)
    CreateModel(
        name="Membership",
        fields=[
            ("id", fields.IntField(primary_key=True)),
            ("person", ForeignKeyFieldInstance("models.Person", related_name="membership_rows")),
            ("group", ForeignKeyFieldInstance("models.Group", related_name="membership_rows")),
        ],
    ).state_forward("models", state)

    person = state.apps.get_model("models.Person")
    group = state.apps.get_model("models.Group")
    membership = state.apps.get_model("models.Membership")

    m2m_field = cast(ManyToManyFieldInstance, person._meta.fields_map["groups"])
    assert m2m_field.through == "membership"
    # NOT resolved to the live `membership` class - kept as the original "app.Model" string the
    # field was declared with, the same way `model_name` is never overwritten with
    # `related_model` (see ManyToManyFieldInstance.__init__'s own comment on this).
    assert m2m_field.through_model == "models.Membership"
    assert m2m_field.forward_keys == ("group_id",)
    assert m2m_field.backward_keys == ("person_id",)

    person_fk = cast(ForeignKeyFieldInstance, membership._meta.fields_map["person"])
    group_fk = cast(ForeignKeyFieldInstance, membership._meta.fields_map["group"])
    assert person_fk.related_model is person
    assert group_fk.related_model is group


def test_alter_options(state_with_model: State):
    operation = AlterModelOptions(name="TestModel", options={"ordering": ["-id"]})
    operation.state_forward("models", state_with_model)

    model_state = state_with_model.models[("models", "TestModel")]
    assert model_state.options["ordering"] == ["-id"]


def test_alter_options_removes_the_options_left_out(empty_state: State):
    """AlterModelOptions carries the model's whole set of generic options - one left out of it was
    removed from Meta, not kept (a kept one re-generated the same migration forever)."""
    CreateModel(
        name="Item",
        fields=[("id", fields.IntField(primary_key=True)), ("deleted_at", fields.DatetimeField(null=True))],
        options={"table": "item", "table_description": "Items", "soft_delete_field": "deleted_at"},
    ).state_forward("models", empty_state)

    AlterModelOptions(name="Item", options={"soft_delete_field": "deleted_at"}).state_forward("models", empty_state)

    model_state = empty_state.models[("models", "Item")]
    assert "table_description" not in model_state.options
    assert model_state.options["soft_delete_field"] == "deleted_at"
    assert model_state.options["table"] == "item"
    assert empty_state.apps.get_model("models.Item")._meta.table_description == ""


def test_rename_model_with_related_name_relations_re_renders_the_targets(empty_state: State):
    """The target of a renamed model's FK/M2M still carried the backward relation the old class
    registered - the renamed class registering the same related_name collided with it."""
    CreateModel(name="Tag", fields=[("id", fields.IntField(primary_key=True))]).state_forward("models", empty_state)
    CreateModel(
        name="Author",
        fields=[
            ("id", fields.IntField(primary_key=True)),
            ("tag", fields.ForeignKeyField("models.Tag", related_name="authors")),
            ("tags", fields.ManyToManyField("models.Tag", related_name="tagged_authors")),
        ],
    ).state_forward("models", empty_state)

    RenameModel(old_name="Author", new_name="Writer").state_forward("models", empty_state)

    tag = empty_state.apps.get_model("models.Tag")
    writer = empty_state.apps.get_model("models.Writer")
    assert cast(RelationalField, tag._meta.fields_map["authors"]).related_model is writer
    assert cast(RelationalField, tag._meta.fields_map["tagged_authors"]).related_model is writer


def test_add_field(state_with_model: State):
    operation = AddField(model_name="TestModel", name="name", field=fields.TextField())
    operation.state_forward("models", state_with_model)

    model_state = state_with_model.models[("models", "TestModel")]
    assert isinstance(model_state.fields["name"], fields.TextField)

    model = state_with_model.apps.get_model("models.TestModel")
    assert isinstance(model._meta.fields_map.get("name"), fields.TextField)


@pytest.mark.parametrize(
    ["field_class", "backward_field_class"],
    [
        (ForeignKeyFieldInstance, BackwardForeignKeyRelation),
        (ManyToManyFieldInstance, ManyToManyFieldInstance),
        (OneToOneFieldInstance, BackwardOneToOneRelation),
    ],
)
def test_add_field_relational(
    state_with_two_models: State,
    field_class: type[ForeignKeyFieldInstance | OneToOneFieldInstance | ManyToManyFieldInstance],
    backward_field_class: type[BackwardForeignKeyRelation | BackwardOneToOneRelation | ManyToManyFieldInstance],
):
    state = state_with_two_models

    operation = AddField(
        model_name="TestModel2",
        name="ref",
        field=field_class("models.TestModel", related_name="child"),
    )
    operation.state_forward("models", state)

    model_state = state.models["models", "TestModel2"]
    field = cast(RelationalFieldInstance, model_state.fields["ref"])
    assert isinstance(field, field_class)
    assert field.model_name == "models.TestModel"

    model = state.apps.get_model("models.TestModel")
    model2 = state.apps.get_model("models.TestModel2")
    field_on_model = cast(RelationalField, model2._meta.fields_map["ref"])
    assert field_on_model.related_model == model

    backward_field = model._meta.fields_map["child"]
    assert isinstance(backward_field, backward_field_class)


def test_remove_field(state_with_model: State):
    operation = AddField(model_name="TestModel", name="name", field=fields.TextField())
    operation.state_forward("models", state_with_model)

    operation = RemoveField(model_name="TestModel", name="name")
    operation.state_forward("models", state_with_model)

    model_state = state_with_model.models[("models", "TestModel")]
    assert not model_state.fields.get("name")

    model = state_with_model.apps.get_model("models.TestModel")
    assert not model._meta.fields_map.get("name")


@pytest.mark.parametrize(
    "field_class",
    [ForeignKeyFieldInstance, ManyToManyFieldInstance, OneToOneFieldInstance],
)
def test_remove_field_relational(
    state_with_two_models: State,
    field_class: type[ForeignKeyFieldInstance | OneToOneFieldInstance | ManyToManyFieldInstance],
):
    state = state_with_two_models

    operation = AddField(
        model_name="TestModel2",
        name="ref",
        field=field_class("models.TestModel", related_name="child"),
    )
    operation.state_forward("models", state)

    operation = RemoveField(
        model_name="TestModel2",
        name="ref",
    )
    operation.state_forward("models", state)
    assert not state.models["models", "TestModel2"].fields.get("ref")

    model = state.apps.get_model("models.TestModel")
    model2 = state.apps.get_model("models.TestModel2")
    assert not model2._meta.fields_map.get("ref")
    assert not model._meta.fields_map.get("child")


def test_alter_field(state_with_model: State):
    operation = AddField(model_name="TestModel", name="name", field=fields.CharField(max_length=255))
    operation.state_forward("models", state_with_model)

    operation = AlterField(
        model_name="TestModel",
        name="name",
        field=fields.CharField(max_length=255, unique=True),
    )
    operation.state_forward("models", state_with_model)

    model_state = state_with_model.models[("models", "TestModel")]
    assert model_state.fields["name"].unique

    model = state_with_model.apps.get_model("models.TestModel")
    assert model._meta.fields_map["name"].unique


# --- RenameField's Meta-level field-name bookkeeping (pk/unique_together/indexes/constraints) ---


def test_rename_field_updates_pk_field_name(empty_state: State):
    """Renaming the PK field must be reflected in both pk_field_name and options["primary_key_attribute"] -
    otherwise a later autodetector run sees a stale old-name pk_field_name on the projected
    state and falsely believes the primary key itself changed."""
    state = empty_state
    CreateModel(name="TestModel", fields=[("id", fields.IntField(primary_key=True))]).state_forward("models", state)

    RenameField(model_name="TestModel", old_name="id", new_name="pk_id").state_forward("models", state)

    model_state = state.models[("models", "TestModel")]
    assert model_state.pk_field_name == "pk_id"
    assert model_state.options["primary_key_attribute"] == "pk_id"
    assert "id" not in model_state.fields
    assert "pk_id" in model_state.fields


def test_rename_field_updates_composite_pk_field_name(empty_state: State):
    """A composite pk_field_name/primary_key_attribute is a tuple - renaming one of its component fields must
    rewrite that field's entry within the tuple, not just a whole-tuple string match."""
    state = empty_state
    CreateModel(
        name="TestModel",
        fields=[("thing_id", fields.IntField()), ("revision", fields.IntField())],
        options={"primary_key_attribute": ("thing_id", "revision")},
    ).state_forward("models", state)

    RenameField(model_name="TestModel", old_name="revision", new_name="version").state_forward("models", state)

    model_state = state.models[("models", "TestModel")]
    assert model_state.pk_field_name == ("thing_id", "version")
    assert model_state.options["primary_key_attribute"] == ("thing_id", "version")


def test_rename_field_updates_unique_together(empty_state: State):
    """A renamed field that participates in unique_together must have that entry rewritten -
    otherwise a later diff sees the live model's (correct, new-name) unique_together against the
    projected state's stale old-name entry and treats it as a changed constraint."""
    state = empty_state
    CreateModel(
        name="TestModel",
        fields=[
            ("id", fields.IntField(primary_key=True)),
            ("email", fields.TextField()),
            ("tenant", fields.TextField()),
        ],
        options={"constraints": [UniqueConstraint(fields=("email", "tenant"))]},
    ).state_forward("models", state)

    RenameField(model_name="TestModel", old_name="email", new_name="address").state_forward("models", state)

    model_state = state.models[("models", "TestModel")]
    assert [tuple(constraint.fields) for constraint in model_state.options["constraints"]] == [("address", "tenant")]


def test_rename_field_updates_index_fields(empty_state: State):
    """A renamed field referenced from a Meta.indexes Index(fields=...) entry must have that
    entry rewritten, and the rename must not mutate the original Index object in place (it may
    still be referenced by an already-cloned "old state" that must keep seeing the old name)."""
    state = empty_state
    original_index = Index(fields=("sku",), name="idx_sku")
    CreateModel(
        name="TestModel",
        fields=[("id", fields.IntField(primary_key=True)), ("sku", fields.TextField())],
        options={"indexes": (original_index,)},
    ).state_forward("models", state)

    RenameField(model_name="TestModel", old_name="sku", new_name="code").state_forward("models", state)

    model_state = state.models[("models", "TestModel")]
    indexes = model_state.options["indexes"]
    assert len(indexes) == 1
    assert list(indexes[0].fields) == ["code"]
    assert indexes[0].name == "idx_sku"
    # The original Index instance (which a cloned "old state" might still hold onto) is
    # untouched by the rename.
    assert list(original_index.fields) == ["sku"]


def test_rename_field_updates_plain_tuple_index_entry(empty_state: State):
    """Meta.indexes also accepts a bare field-name tuple/list shorthand (no explicit Index(...))
    - that form must be rewritten too, not just real Index instances."""
    state = empty_state
    CreateModel(
        name="TestModel",
        fields=[("id", fields.IntField(primary_key=True)), ("sku", fields.TextField())],
        options={"indexes": (("sku",),)},
    ).state_forward("models", state)

    RenameField(model_name="TestModel", old_name="sku", new_name="code").state_forward("models", state)

    model_state = state.models[("models", "TestModel")]
    assert model_state.options["indexes"] == (("code",),)


def test_rename_field_updates_unique_constraint(empty_state: State):
    """A renamed field referenced from a Meta.constraints UniqueConstraint must have its
    `fields` tuple rewritten - UniqueConstraint is a frozen dataclass, so this must produce a
    new instance rather than mutate the existing (possibly still-shared) one in place."""
    state = empty_state
    original_constraint = UniqueConstraint(fields=("email",), name="uq_email")
    CreateModel(
        name="TestModel",
        fields=[("id", fields.IntField(primary_key=True)), ("email", fields.TextField())],
        options={"constraints": (original_constraint,)},
    ).state_forward("models", state)

    RenameField(model_name="TestModel", old_name="email", new_name="address").state_forward("models", state)

    model_state = state.models[("models", "TestModel")]
    constraints = model_state.options["constraints"]
    assert len(constraints) == 1
    assert constraints[0].fields == ("address",)
    assert constraints[0].name == "uq_email"
    assert original_constraint.fields == ("email",)


def test_rename_field_leaves_unrelated_meta_bookkeeping_untouched(empty_state: State):
    """Renaming a field that plays no part in the PK/unique_together/indexes must not perturb
    any of them."""
    state = empty_state
    CreateModel(
        name="TestModel",
        fields=[
            ("id", fields.IntField(primary_key=True)),
            ("email", fields.TextField()),
            ("notes", fields.TextField()),
        ],
        options={"constraints": [UniqueConstraint(fields=("email",))]},
    ).state_forward("models", state)

    RenameField(model_name="TestModel", old_name="notes", new_name="comment").state_forward("models", state)

    model_state = state.models[("models", "TestModel")]
    assert model_state.pk_field_name == "id"
    assert model_state.options["primary_key_attribute"] == "id"
    assert [tuple(constraint.fields) for constraint in model_state.options["constraints"]] == [("email",)]
