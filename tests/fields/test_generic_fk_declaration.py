"""GenericForeignKeyField declarations: what is refused, with which message, and how an abstract
base hands the field to every concrete model."""

from __future__ import annotations

from typing import Any

import pytest

from hare import Hare, fields
from hare.ddl.conditions.exclusive_arc_condition import ExclusiveArcCondition
from hare.exceptions import ConfigurationError
from hare.fields import CASCADE, SET_DEFAULT, SET_NULL
from hare.fields.relations.fields import GenericForeignKeyFieldInstance
from hare.models import Model, swappable
from tests.fields.models_generic_fk import Photo, Post


def build_model(name: str, **attributes: Any) -> type[Model]:
    return type(name, (Model,), {"__module__": __name__, "id": fields.IntField(primary_key=True), **attributes})


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        (({},), "at least one target"),
        (({"1post": Post},), "isn't a Python identifier"),
        (({"class": Post},), "isn't a Python identifier"),
        (({"post": Post, "again": Post},), "name the same model"),
        (({"post": "models.Post", "again": "models.Post"},), "name the same model"),
        (({"post": "Post"},), 'accepts model name in format "app.Model"'),
        (({"post": 5},), "is invalid"),
        (([Post, Photo],), "takes a dict of branch name to model"),
    ],
)
def test_the_targets_are_checked(arguments, message):
    with pytest.raises(ConfigurationError, match=message):
        fields.GenericForeignKeyField(*arguments)


def test_the_options_are_checked():
    with pytest.raises(ConfigurationError, match="takes no to_field"):
        fields.GenericForeignKeyField({"post": Post}, to_field="id")
    with pytest.raises(ConfigurationError, match="SET_NULL must be null=True"):
        fields.GenericForeignKeyField({"post": Post}, on_delete=SET_NULL)
    with pytest.raises(ConfigurationError, match="SET_DEFAULT needs a default"):
        fields.GenericForeignKeyField({"post": Post}, on_delete=SET_DEFAULT)
    with pytest.raises(ConfigurationError, match="must be bools"):
        fields.GenericForeignKeyField({"post": Post}, null="yes")  # type: ignore[arg-type]
    with pytest.raises(ConfigurationError, match="lazy can only be"):
        fields.GenericForeignKeyField({"post": Post}, lazy="eager")  # type: ignore[arg-type]


def test_a_branch_taking_the_name_of_another_attribute_is_refused():
    with pytest.raises(ConfigurationError, match="the branch 'post' is the name of another attribute"):
        build_model(
            "TakenBranch", post=fields.CharField(max_length=5), target=fields.GenericForeignKeyField({"post": Post})
        )
    with pytest.raises(ConfigurationError, match="the branch 'target' is the name of another attribute"):
        build_model("SelfNamedBranch", target=fields.GenericForeignKeyField({"target": Post}))


def test_the_model_gets_the_branches_the_check_and_a_descriptor():
    model = build_model(
        "Declared", target=fields.GenericForeignKeyField({"post": Post, "photo": "models.Photo"}, null=True)
    )
    meta = model._meta
    assert list(meta.fields_map) == ["id", "post", "photo"]
    assert isinstance(model.target, GenericForeignKeyFieldInstance)
    assert model.target.get_label() == "Declared.target"
    [check] = [constraint for constraint in meta.constraints if constraint.name == "target_exclusive_arc"]
    assert check.check == ExclusiveArcCondition(("post", "photo"), allow_none=True)
    assert meta.fields_map["post"].index and meta.fields_map["post"].null
    assert meta.fields_map["post"].generic_relation is model.target
    assert model.target.deconstruct()[2] == {"null": True}


def test_an_abstract_base_hands_the_field_to_every_concrete_model():
    base = type(
        "Commentable",
        (Model,),
        {
            "__module__": __name__,
            "target": fields.GenericForeignKeyField({"post": Post, "photo": Photo}, on_delete=CASCADE),
            "Meta": type("Meta", (), {"abstract": True}),
        },
    )
    assert "post" not in base._meta.fields_map
    first = type("FirstConcrete", (base,), {"__module__": __name__, "id": fields.IntField(primary_key=True)})
    second = type("SecondConcrete", (base,), {"__module__": __name__, "id": fields.IntField(primary_key=True)})
    assert {"post", "photo"} <= set(first._meta.fields_map)
    assert first.target is not second.target
    assert first.target.model is first and second.target.model is second
    assert first._meta.fields_map["post"] is not second._meta.fields_map["post"]


def test_a_swappable_dict_of_targets_waits_for_the_configuration():
    model = build_model("SwappableTargets", target=fields.GenericForeignKeyField(swappable("SOME_TARGETS")))
    assert "target" in model._meta.generic_foreign_key_fields
    assert model.target.branch_names == ()
    assert [name for name in model._meta.fields_map] == ["id"]


@pytest.mark.asyncio
async def test_a_target_registered_live_is_refused(db_generic_fk):
    live_target = build_model("LiveTarget", Meta=type("Meta", (), {"table": "generic_live_target"}))
    live_comment = build_model(
        "LiveComment",
        target=fields.GenericForeignKeyField({"live_target": live_target}),
        Meta=type("Meta", (), {"table": "generic_live_comment"}),
    )
    with pytest.raises(ConfigurationError, match="was registered by register_live_models"):
        Hare.register_live_models([live_target, live_comment], "live", connection_alias="models")
