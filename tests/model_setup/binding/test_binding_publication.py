from types import ModuleType

import pytest

from hare import Model, fields
from hare.core.apps.model_binding_graph import ModelBindingGraph
from hare.core.hare_context import HareContext
from hare.exceptions import ConfigurationError
from hare.query.queryset.pending_calls.calls_before_setup import CallsBeforeSetup

pytestmark = [pytest.mark.database_independent, pytest.mark.asyncio]


async def test_failed_registration_preserves_metadata_and_subclass_attributes():
    class Parent(Model):
        value = fields.TextField()

    class Descendant(Parent):
        pass

    class FirstChild(Model):
        parent = fields.ForeignKeyField("binding.Parent", related_name="children")

    class SecondChild(Model):
        parent = fields.ForeignKeyField("binding.Parent", related_name="children")

    module = ModuleType("runtime_binding_publication")
    module.__models__ = [Parent]  # type: ignore[attr-defined]
    async with HareContext() as context:
        await context.init(
            {"connections": {"default": "sqlite+aiosqlite://:memory:"}, "apps": {"binding": {"models": [module]}}},
            connect=False,
        )
        models = (Parent, Descendant, FirstChild, SecondChild)
        previous = {model: dict(vars(model)) for model in models}
        registry = context.apps.apps
        with pytest.raises(ConfigurationError, match="duplicates"):
            context.register_live_models([FirstChild, SecondChild], "binding")

        assert context.apps.apps is registry
        for model, attributes in previous.items():
            assert vars(model).keys() == attributes.keys()
            assert all(vars(model)[name] is value for name, value in attributes.items())
        assert ModelBindingGraph.current.get() is None

        context.register_live_models([FirstChild], "binding")
        previous_parent_meta = previous[Parent]["_meta"]
        assert "children" not in previous_parent_meta.fields_map
        assert "children" in Parent._meta.fields_map
        relation = FirstChild._meta.fields_map["parent"]
        assert relation.to_field_instance is Parent._meta.fields_map["id"]
        assert FirstChild._meta.fields_map["parent_id"].reference is relation

        context.unregister_live_models([FirstChild])
        assert "children" not in Parent._meta.fields_map
        assert not hasattr(Descendant, "children")


async def test_failed_reinitialization_preserves_graph_connections_and_router(monkeypatch):
    monkeypatch.setattr(CallsBeforeSetup, "PENDING", [])

    class Existing(Model):
        value = fields.TextField()

    class Added(Model):
        value = fields.TextField()

    initial_module = ModuleType("initial_binding_publication")
    initial_module.__models__ = [Existing]  # type: ignore[attr-defined]
    replacement_module = ModuleType("replacement_binding_publication")
    replacement_module.__models__ = [Existing, Added]  # type: ignore[attr-defined]
    config = {
        "connections": {"default": "sqlite+aiosqlite://:memory:"},
        "apps": {"binding": {"models": [initial_module]}},
    }
    async with HareContext() as context:
        await context.init(config, connect=False)
        previous = (Existing._meta, Added._meta, context.apps, context.connections, context.router)
        valid = Added.objects.filter(value="kept")
        invalid = Added.objects.filter(missing="bad")
        pending = list(CallsBeforeSetup.PENDING)
        states = [query._calls_before_setup for query in pending]
        replacement = {**config, "apps": {"binding": {"models": [replacement_module]}}}
        with pytest.raises(ConfigurationError, match="Queries built before"):
            await context.init(replacement, connect=False)
        assert all(
            current is original
            for current, original in zip(
                (Existing._meta, Added._meta, context.apps, context.connections, context.router), previous, strict=True
            )
        )
        assert CallsBeforeSetup.PENDING == pending
        assert [query._calls_before_setup for query in pending] == states
        assert valid._calls_before_setup is not None
        CallsBeforeSetup.PENDING.remove(invalid)
        await context.init(replacement, connect=False)
        assert valid._calls_before_setup is None
        assert Added._meta._inited


async def test_table_identity_includes_connection_schema_and_dialect_case_rules():
    class Item(Model):
        pass

    module = ModuleType("physical_binding_publication")
    module.__models__ = [Item]  # type: ignore[attr-defined]
    async with HareContext() as context:
        await context.init(
            {
                "connections": {
                    "default": "sqlite+aiosqlite://:memory:",
                    "other": "sqlite+aiosqlite://:memory:",
                },
                "apps": {"binding": {"models": [module], "default_connection": "default"}},
            },
            connect=False,
        )
        identity = context.apps.get_table_identity(Item, "Tenant", "Entries")
        assert identity == context.apps.get_table_identity(Item, "tenant", "entries")
        assert identity != context.apps.get_table_identity(Item, "another", "entries")
        Item._meta.default_connection = "other"
        assert identity != context.apps.get_table_identity(Item, "Tenant", "Entries")
