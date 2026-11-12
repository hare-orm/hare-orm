"""The keys a ManyToManyField declares are kept apart from the through-table columns linking fills in: a
composite key's declared prefix is written into a migration as declared, and a copy of the linked field
expands it once - never the first column taken for a prefix again."""

import sys
import types

import pytest

from hare import Hare, fields
from hare.contrib.test.isolated_contexts import hare_test_context
from hare.core.config import HareConfig
from hare.exceptions import ConfigurationError
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


@pytest.fixture
def clashing_keys_module():
    """A module of a many-to-many relation whose two sides are named "key" and "KEY"."""

    class ClashTarget(Model):
        id = fields.IntField(primary_key=True)

        class Meta:
            app = "clash"

    class ClashOwner(Model):
        id = fields.IntField(primary_key=True)
        targets = fields.ManyToManyField("clash.ClashTarget", forward_key="key", backward_key="KEY")

        class Meta:
            app = "clash"

    module_name = "tests._many_to_many_clashing_keys"
    module = types.ModuleType(module_name)
    module.ClashTarget, module.ClashOwner = ClashTarget, ClashOwner  # type: ignore[attr-defined]
    sys.modules[module_name] = module
    yield module_name
    sys.modules.pop(module_name, None)


@pytest.mark.asyncio
async def test_two_sides_of_one_column_name_are_refused(clashing_keys_module):
    """forward_key and backward_key of one name gave the automatic through table that column twice - its
    CREATE TABLE failed on the database. SQLite tells "key" and "KEY" apart by nothing."""
    with pytest.raises(ConfigurationError, match="forward_key and backward_key name the same column"):
        async with hare_test_context(modules=[clashing_keys_module], app_label="clash", _generate_schemas=False):
            pass


def test_two_sides_whose_names_differ_in_case_are_kept_where_the_database_tells_them_apart(clashing_keys_module):
    """The sides were compared ignoring case on every database - "key" and "KEY", two columns of PostgreSQL,
    were refused as one."""
    apps = Hare.bind_models(
        HareConfig.from_db_url("postgresql://user:password@127.0.0.1:1/database", {"clash": [clashing_keys_module]})
    )

    field = apps.get_model("clash.ClashOwner")._meta.fields_map["targets"]
    assert (field.forward_keys, field.backward_keys) == (("key",), ("KEY",))
