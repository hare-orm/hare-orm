import pytest

from hare import Model, fields
from hare.core.apps import Apps
from hare.core.hare_context import HareContext
from hare.exceptions import ConfigurationError
from hare.fields.relations.swappable_model_reference import swappable
from hare.migrations.state.model_state import ModelState
from hare.migrations.state.state import State

pytestmark = pytest.mark.database_independent


def test_deferred_rendering_and_clones_keep_the_captured_application():
    class Account(Model):
        class Meta:
            app = "app"
            swappable = "ACCOUNT"

    class External(Model):
        note = fields.CharField(max_length=20)

        class Meta:
            app = "app"
            managed = False

    class Event(Model):
        account = fields.ForeignKeyField(swappable("ACCOUNT"))
        external = fields.ForeignKeyField("app.External")

        class Meta:
            app = "app"

    first = HareContext()
    first._apps = Apps({}, first.connections, swappable={"ACCOUNT": "app.Account"})
    first._apps.apps = {"app": {"Account": Account, "External": External}}
    token = HareContext.current_context.set(first)
    try:
        state = State.from_models([Account, Event])
    finally:
        HareContext.current_context.reset(token)
    second = HareContext()
    second._apps = Apps({}, second.connections, swappable={"ACCOUNT": "other.Missing"})
    first._apps.swappable_settings["ACCOUNT"] = "app.Missing"
    External._meta.fields_map["note"].max_length = 100
    token = HareContext.current_context.set(second)
    try:
        clone = state.clone()
        assert clone.environment is state.environment
        for candidate in (state, clone):
            assert candidate.get_swappable_label("ACCOUNT") == "app.Account"
            apps = candidate.apps
            assert apps._connections is first.connections
            event = apps.get_model("app.Event")
            assert event._meta.fields_map["account"].related_model is apps.get_model("app.Account")
            assert apps.get_model("app.External")._meta.fields_map["note"].max_length == 20
    finally:
        HareContext.current_context.reset(token)


def test_failed_rendering_preserves_classes_and_change_marks_for_retry():
    class Parent(Model):
        class Meta:
            app = "app"

    class Child(Model):
        parent = fields.ForeignKeyField("app.Parent", related_name="id")

        class Meta:
            app = "app"

    state = State.from_models([Parent])
    apps = state.apps
    parent = apps.get_model("app.Parent")
    previous_meta = parent._meta
    previous_registry = apps.apps
    child_key = ("app", "Child")
    child_state = ModelState.make_from_model("app", Child)
    state.models[child_key] = child_state
    state.mark_model_added(*child_key)

    with pytest.raises(ConfigurationError, match="duplicates"):
        state.apps
    assert apps.apps is previous_registry
    assert parent._meta is previous_meta
    assert "Child" not in apps.apps["app"]
    assert state.rendering.added_keys == {child_key}

    child_state.set_field("parent", fields.ForeignKeyField("app.Parent", related_name="children"))
    assert state.apps is apps
    child = apps.get_model("app.Child")
    assert "children" not in previous_meta.fields_map
    assert "children" in parent._meta.fields_map
    assert child._meta.fields_map["parent"].related_model is parent
    assert not state.rendering.added_keys

    previous_child_meta = child._meta
    child_state.set_field("parent", fields.ForeignKeyField("app.Parent", to_field="missing"))
    state.mark_models_changed([child_key])
    with pytest.raises(ConfigurationError, match="no field named 'missing'"):
        state.apps
    assert apps.get_model("app.Child") is child
    assert child._meta is previous_child_meta
    assert state.rendering.changed_keys == {child_key}

    child_state.set_field("parent", fields.ForeignKeyField("app.Parent", related_name="children"))
    assert state.apps.get_model("app.Child") is not child
    assert not state.rendering.changed_keys
