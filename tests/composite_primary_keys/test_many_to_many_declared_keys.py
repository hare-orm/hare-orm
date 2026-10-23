"""The keys a ManyToManyField declares are kept apart from the through-table columns linking fills in: a
composite key's declared prefix is written into a migration as declared, and a copy of the linked field
expands it once - never the first column taken for a prefix again."""

import pytest

from hare import fields
from hare.contrib.test.isolated_contexts import hare_test_context
from hare.migrations.state.state import State
from hare.models import Model


class DeclaredKeyTarget(Model):
    a = fields.IntField()
    b = fields.IntField()

    pk = fields.CompositePrimaryKey("a", "b")


class DeclaredKeyOwner(Model):
    a = fields.IntField()
    b = fields.IntField()
    targets: fields.ManyToManyRelation[DeclaredKeyTarget] = fields.ManyToManyField(
        "models.DeclaredKeyTarget", backward_key="owner", forward_key="target"
    )

    pk = fields.CompositePrimaryKey("a", "b")


@pytest.mark.asyncio
async def test_a_composite_key_prefix_is_written_and_expanded_as_declared():
    async with hare_test_context(modules=[__name__], app_label="models", _generate_schemas=False):
        field = DeclaredKeyOwner._meta.fields_map["targets"]
        assert field.backward_keys == ("owner_a", "owner_b")
        assert field.forward_keys == ("target_a", "target_b")

        _path, _args, kwargs = field.deconstruct()
        assert (kwargs["backward_key"], kwargs["forward_key"]) == ("owner", "target")

        rendered_owner = State.from_models([DeclaredKeyTarget, DeclaredKeyOwner]).apps.get_model(
            "models.DeclaredKeyOwner"
        )
        rendered_field = rendered_owner._meta.fields_map["targets"]
        assert rendered_field.backward_keys == ("owner_a", "owner_b")
        assert rendered_field.forward_keys == ("target_a", "target_b")
