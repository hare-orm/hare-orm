"""Native resources are applied in the same database namespace used by autodetection."""

import os

import pytest

from hare.contrib.test import requires_features
from hare.contrib.test.databases.temporary_databases import TemporaryDatabases
from hare.migrations.execution.connections_plan import ConnectionsPlan
from hare.migrations.execution.executor.migration_executor import MigrationExecutor
from tests.contracts.native_prerequisite_samples import NativePrerequisiteSamples


@requires_features(supports_enum_types=True)
@pytest.mark.asyncio
@pytest.mark.parametrize("shared", [False, True], ids=["separate-databases", "shared-database"])
async def test_later_app_can_apply_its_native_prerequisites_without_changing_existing_rows(contract_database, shared):
    aliases = {"alpha": "left", "beta": "left" if shared else "right", "gamma": "right"}
    config = NativePrerequisiteSamples.get_config(aliases)
    initial = NativePrerequisiteSamples.get_state(aliases, ("alpha",))
    previous = NativePrerequisiteSamples.get_history(await NativePrerequisiteSamples.get_changes(initial))
    current = NativePrerequisiteSamples.get_state(aliases, ("alpha", "beta"), different=not shared)
    added = NativePrerequisiteSamples.get_history(await NativePrerequisiteSamples.get_changes(current, previous))
    definitions = NativePrerequisiteSamples.get_definitions([*previous, *added])
    async with TemporaryDatabases(
        {"connections": {"left": None, "right": None}, "apps": {}},
        os.environ["HARE_TEST_DB"],
        generate_schemas=False,
        reuse_databases=False,
    ) as context:
        left = context.get_connection("left")
        await MigrationExecutor(
            left,
            {"alpha": config["alpha"]},
            full_apps_config=config,
            definitions=NativePrerequisiteSamples.get_definitions(previous),
        ).migrate()
        await left.execute_script("INSERT INTO prerequisite_alpha(id, status) VALUES (1, 'open')")
        executors = {}
        for alias in {aliases["alpha"], aliases["beta"]}:
            apps = {app: config[app] for app in ("alpha", "beta") if aliases[app] == alias}
            executors[alias] = MigrationExecutor(
                context.get_connection(alias), apps, full_apps_config=config, definitions=definitions
            )
        await ConnectionsPlan(executors).migrate()
        right = context.get_connection(aliases["beta"])
        status = "open" if shared else "yes"
        await right.execute_script(f"INSERT INTO prerequisite_beta(id, status) VALUES (1, '{status}')")
        assert await left.execute_dicts("SELECT id, status::text AS status FROM prerequisite_alpha") == [
            {"id": 1, "status": "open"}
        ]
        assert await right.execute_dicts("SELECT id, status::text AS status FROM prerequisite_beta") == [
            {"id": 1, "status": status}
        ]
