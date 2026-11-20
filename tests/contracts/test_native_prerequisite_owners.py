"""Native schemas/types are shared on one connection and independent across connections."""

import pytest

from hare import fields
from hare.dialects.postgresql.fields.citext_field import CitextField
from hare.exceptions import ConfigurationError
from hare.migrations import State
from hare.migrations.operations import (
    AlterEnumType,
    CreateEnumType,
    CreateExtension,
    CreateSchema,
    DropEnumType,
    DropSchema,
    RemoveExtension,
)
from hare.models.enums import ModelOption
from tests.contracts.native_prerequisite_samples import NativePrerequisiteSamples

pytestmark = pytest.mark.database_independent


@pytest.mark.asyncio
@pytest.mark.parametrize("different", [False, True], ids=["same-labels", "different-labels"])
@pytest.mark.parametrize("existing", [False, True], ids=["fresh", "existing-first"])
async def test_independent_connections_each_create_native_prerequisites(different, existing):
    aliases = {"alpha": "left", "beta": "right", "gamma": "right"}
    previous = []
    if existing:
        initial = NativePrerequisiteSamples.get_state(aliases, ("alpha",))
        previous = NativePrerequisiteSamples.get_history(await NativePrerequisiteSamples.get_changes(initial))
    current = NativePrerequisiteSamples.get_state(aliases, ("alpha", "beta"), different=different)
    writers = await NativePrerequisiteSamples.get_changes(current, previous)
    (beta,) = [writer for writer in writers if writer.app_label == "beta"]
    assert sum(isinstance(operation, CreateSchema) for operation in beta.operations) == 1
    assert sum(isinstance(operation, CreateEnumType) for operation in beta.operations) == 1
    assert all(app != "alpha" for app, _name in beta.dependencies)


@pytest.mark.asyncio
async def test_shared_connection_emits_one_provider_and_reloads_its_history():
    aliases = dict.fromkeys(NativePrerequisiteSamples.apps, "shared")
    state = NativePrerequisiteSamples.get_state(aliases, ("alpha", "beta"))
    writers = await NativePrerequisiteSamples.get_changes(state)
    assert sum(isinstance(operation, CreateEnumType) for writer in writers for operation in writer.operations) == 1
    first = next(writer for writer in writers if writer.app_label == "alpha")
    second = next(writer for writer in writers if writer.app_label == "beta")
    assert (first.app_label, first.name) in second.dependencies
    assert await NativePrerequisiteSamples.get_changes(state, NativePrerequisiteSamples.get_history(writers)) == []


@pytest.mark.asyncio
async def test_conflicting_labels_still_fail_on_a_shared_connection():
    aliases = dict.fromkeys(NativePrerequisiteSamples.apps, "shared")
    state = NativePrerequisiteSamples.get_state(aliases, ("alpha", "beta"), different=True)
    with pytest.raises(ConfigurationError, match="different labels"):
        await NativePrerequisiteSamples.get_changes(state)


@pytest.mark.asyncio
@pytest.mark.parametrize("uses_extension", [False, True], ids=["schema", "extension"])
@pytest.mark.parametrize("shared", [False, True], ids=["independent-databases", "shared-database"])
async def test_schemas_and_extensions_share_the_native_namespace_rules(uses_extension, shared):
    aliases = {"alpha": "left", "beta": "left" if shared else "right", "gamma": "right"}
    initial = NativePrerequisiteSamples.get_state(aliases, ("alpha",))
    current = NativePrerequisiteSamples.get_state(aliases, ("alpha", "beta"))
    for state in (initial, current):
        for model in state.models.values():
            model.fields["status"] = CitextField() if uses_extension else fields.IntField()
            if not uses_extension:
                model.options[ModelOption.SCHEMA] = "shared schema"
        state.mark_models_changed(state.models)
    previous = NativePrerequisiteSamples.get_history(await NativePrerequisiteSamples.get_changes(initial))
    writers = await NativePrerequisiteSamples.get_changes(current, previous)
    (beta,) = [writer for writer in writers if writer.app_label == "beta"]
    operation_type = CreateExtension if uses_extension else CreateSchema
    assert any(isinstance(operation, operation_type) for operation in beta.operations) is not shared
    assert any(app == "alpha" for app, _name in beta.dependencies) is shared


def test_native_inventory_and_provenance_are_independent_in_a_state_clone():
    source = State(default_connections={"alpha": "left", "beta": "right"})
    for operation in (CreateSchema("shared"), CreateExtension("citext"), CreateEnumType("status", ("a", "b"))):
        operation.state_forward("alpha", source)
    copied = source.clone()
    for operation in (DropSchema("shared"), RemoveExtension("citext"), DropEnumType("status", ("a", "b"))):
        operation.state_forward("alpha", copied)
    assert source.schemas == {("left", "shared")}
    assert source.extensions == {("left", "citext")}
    assert set(source.enum_types) == {("left", None, "status")}
    assert len(source.native_providers) == 3
    assert copied.schemas == copied.extensions == set()
    assert copied.enum_types == copied.native_providers == {}


@pytest.mark.asyncio
async def test_a_later_consumer_depends_on_the_native_providers_existing_migration():
    aliases = dict.fromkeys(NativePrerequisiteSamples.apps, "shared")
    initial = NativePrerequisiteSamples.get_state(aliases, ("alpha",))
    (first,) = await NativePrerequisiteSamples.get_changes(initial)
    current = NativePrerequisiteSamples.get_state(aliases, ("alpha", "beta"))
    (second,) = await NativePrerequisiteSamples.get_changes(current, NativePrerequisiteSamples.get_history([first]))
    assert second.app_label == "beta"
    assert (first.app_label, first.name) in second.dependencies
    assert not any(isinstance(operation, (CreateSchema, CreateEnumType)) for operation in second.operations)


@pytest.mark.asyncio
async def test_removing_the_last_consumers_drops_the_shared_type_once():
    aliases = dict.fromkeys(NativePrerequisiteSamples.apps, "shared")
    initial = NativePrerequisiteSamples.get_state(aliases, ("alpha", "beta"))
    previous = NativePrerequisiteSamples.get_history(await NativePrerequisiteSamples.get_changes(initial))
    empty = NativePrerequisiteSamples.get_state(aliases, ())
    writers = await NativePrerequisiteSamples.get_changes(empty, previous)
    assert sum(isinstance(operation, DropEnumType) for writer in writers for operation in writer.operations) == 1
    assert (
        await NativePrerequisiteSamples.get_changes(
            empty, [*previous, *NativePrerequisiteSamples.get_history(writers)]
        )
        == []
    )


@pytest.mark.asyncio
async def test_native_type_changes_are_dependencies_of_new_consumers_and_future_history():
    aliases = dict.fromkeys(NativePrerequisiteSamples.apps, "shared")
    initial = NativePrerequisiteSamples.get_state(aliases, ("alpha",))
    previous = NativePrerequisiteSamples.get_history(await NativePrerequisiteSamples.get_changes(initial))
    expanded = NativePrerequisiteSamples.get_state(aliases, ("alpha", "beta"), expanded=True)
    writers = await NativePrerequisiteSamples.get_changes(expanded, previous)
    changes = [writer for writer in writers if any(isinstance(op, AlterEnumType) for op in writer.operations)]
    assert len(changes) == 1
    changing = changes[0]
    for writer in writers:
        if writer.app_label != changing.app_label:
            assert (changing.app_label, changing.name) in writer.dependencies
    previous.extend(NativePrerequisiteSamples.get_history(writers))
    latest = NativePrerequisiteSamples.get_state(aliases, NativePrerequisiteSamples.apps, expanded=True)
    (third,) = await NativePrerequisiteSamples.get_changes(latest, previous)
    assert third.app_label == "gamma"
    assert (changing.app_label, changing.name) in third.dependencies
