"""All supported references use captured unmanaged target keys during dependency planning."""

from types import ModuleType

import pytest

from hare import Model, fields
from hare.core.hare_context import HareContext
from hare.migrations.autodetection.migration_splitter import MigrationSplitter
from hare.migrations.autodetection.operation_sorter import OperationSorter
from hare.migrations.making.migration_optimizer import MigrationOptimizer
from hare.migrations.operations import AddField, CreateModel
from hare.migrations.operations.dependencies.relation_dependencies import RelationDependencies
from hare.migrations.state.migration_environment import MigrationEnvironment
from hare.migrations.state.state import State
from hare.models import swappable

pytestmark = [pytest.mark.database_independent, pytest.mark.asyncio]


@pytest.mark.parametrize("target_composite", [False, True])
@pytest.mark.parametrize("source_composite", [False, True])
@pytest.mark.parametrize("one_to_one", [False, True])
@pytest.mark.parametrize("reference", ["label", "swappable", "class", "explicit"])
async def test_planners_use_unmanaged_target_keys_without_mutating_declarations(
    target_composite, source_composite, one_to_one, reference
):
    class External(Model):
        number = fields.IntField(primary_key=not target_composite, generated=False, source_field="wire_number")
        region = fields.IntField(source_field="wire_region")
        if target_composite:
            pk = fields.CompositePrimaryKey("region", "number")

        class Meta:
            managed = False

    module = ModuleType("unmanaged_dependency_models")
    module.__models__ = [External]
    async with HareContext() as context:
        await context.init(
            {
                "connections": {"default": "sqlite+aiosqlite://:memory:"},
                "apps": {"bridge": {"models": [module]}},
                "swappable": {"EXTERNAL_MODEL": "bridge.External"},
            },
            connect=False,
        )
        state = State(
            default_connections={"bridge": "default", "child": "default"},
            environment=MigrationEnvironment.from_apps(context.apps),
        )
        declarations = [
            ("number", fields.IntField(primary_key=not source_composite, generated=False)),
            ("region", fields.IntField()),
        ]
        if source_composite:
            declarations.append(("pk", fields.CompositePrimaryKey("region", "number")))
        CreateModel("Child", declarations).state_forward("child", state)
        target = (
            External
            if reference == "class"
            else swappable("EXTERNAL_MODEL")
            if reference == "swappable"
            else "bridge.External"
        )
        to_field = (("region", "number") if target_composite else "number") if reference == "explicit" else None
        factory = fields.OneToOneField if one_to_one else fields.ForeignKeyField
        operation = AddField("Child", "external", factory(target, to_field=to_field, null=True, related_name=False))
        attributes = RelationDependencies.get_field_attribute_names("child", "external", operation.field, state)
        assert attributes == (
            frozenset(("external", "external_region", "external_number"))
            if target_composite
            else frozenset(("external", "external_id"))
        )
        assert len(MigrationOptimizer("child").optimize([operation], state)) == 1
        assert len(OperationSorter(state).sort([("child", operation)])) == 1
        assert MigrationSplitter(state, ["child", "bridge"], lambda app: [(app, "0001")]).split({"child": [operation]})
        assert "external" not in state.models[("child", "Child")].fields
        assert ("bridge", "External") not in state.models
        assert operation.field.to_field == to_field
