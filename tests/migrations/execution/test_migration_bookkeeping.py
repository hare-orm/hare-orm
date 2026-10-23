"""The small pieces migrations keep their books with: a node of the migration graph, the recorder that
records nothing, the model a ``swappable`` setting points at in a state, and an object change a database
can't make."""

from types import SimpleNamespace

import pytest

from hare.ddl.schema_objects.dictionary import Dictionary
from hare.dialects.dialect_registry import DialectRegistry
from hare.exceptions import ConfigurationError, UnSupportedError
from hare.fields import IntField
from hare.migrations.loading.graph.migration_key import MigrationKey
from hare.migrations.loading.graph.node import Node
from hare.migrations.loading.recorder.noop_recorder import NoopRecorder
from hare.migrations.operations import AlterDictionary, CreateModel
from hare.migrations.state.state import State
from hare.models.enums import ModelOption
from tests.utils.fake_client import FakeClient

pytestmark = pytest.mark.database_independent

APP_LABEL = "shop"


def test_a_node_is_its_migration_key():
    key = MigrationKey("shop", "0001_initial")
    node = Node(key)
    assert node == Node(MigrationKey("shop", "0001_initial"))
    assert node == key
    assert node != Node(MigrationKey("shop", "0002_more"))
    assert str(node) == "shop.0001_initial"
    assert repr(node) == "<Node: ('shop', '0001_initial')>"


@pytest.mark.asyncio
async def test_the_recorder_of_a_plan_records_nothing():
    recorder = NoopRecorder()
    assert await recorder.applied_migrations() == []
    assert await recorder.ensure_schema(SimpleNamespace()) is None


def get_state_declaring(*swappable_settings: str) -> State:
    state = State()
    for index, setting in enumerate(swappable_settings):
        CreateModel(
            name=f"Member{index}",
            fields=[("id", IntField(primary_key=True))],
            options={ModelOption.SWAPPABLE: setting},
        ).state_forward(APP_LABEL, state)
    return state


def test_the_model_of_a_swappable_setting_is_the_one_declaring_it():
    assert get_state_declaring("MEMBER_MODEL").get_declared_swappable_label("MEMBER_MODEL") == "shop.Member0"
    with pytest.raises(ConfigurationError, match="no model declares Meta.swappable = 'MEMBER_MODEL'"):
        get_state_declaring().get_declared_swappable_label("MEMBER_MODEL")
    with pytest.raises(ConfigurationError, match=r"declared by several models \(shop.Member0, shop.Member1\)"):
        get_state_declaring("MEMBER_MODEL", "MEMBER_MODEL").get_declared_swappable_label("MEMBER_MODEL")


def test_an_object_change_a_database_lacks_is_refused_before_it_runs():
    state = State()
    CreateModel(name="Item", fields=[("id", IntField(primary_key=True)), ("label", IntField())]).state_forward(
        APP_LABEL, state
    )
    sqlite = DialectRegistry.get_dialect("sqlite")
    editor = sqlite.schema_editor_class(FakeClient("sqlite"))
    operation = AlterDictionary("Item", Dictionary("item_names", key=("id",), attributes=("label",)))
    with pytest.raises(UnSupportedError, match="not supported on sqlite"):
        operation.raise_if_unsupported(APP_LABEL, state, state, editor)
