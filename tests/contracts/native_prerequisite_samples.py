from __future__ import annotations

from enum import StrEnum

from hare import Model, fields
from hare.dialects.postgresql.fields.native_enum import NativeEnumField
from hare.migrations import Migration, State
from hare.migrations.autodetection.migration_autodetector import MigrationAutodetector
from hare.migrations.loading.graph.migration_key import MigrationKey
from hare.migrations.loading.migration_definitions import MigrationDefinitions
from hare.migrations.state.migration_environment import MigrationEnvironment
from hare.migrations.writer.migration_writer import MigrationWriter


class InitialLabels(StrEnum):
    OPEN = "open"
    CLOSED = "closed"


class OtherLabels(StrEnum):
    YES = "yes"
    NO = "no"


class ExpandedLabels(StrEnum):
    OPEN = "open"
    CLOSED = "closed"
    ARCHIVED = "archived"


class NativePrerequisiteSamples:
    """Offline multi-app declarations and their serialized migration history."""

    apps = ("alpha", "beta", "gamma")

    @staticmethod
    def get_state(
        aliases: dict[str, str],
        selected: tuple[str, ...],
        *,
        different: bool = False,
        expanded: bool = False,
    ) -> State:
        models = []
        for app, name in zip(NativePrerequisiteSamples.apps, ("First", "Second", "Third"), strict=True):
            if app not in selected:
                continue
            labels = ExpandedLabels if expanded else OtherLabels if different and app == "beta" else InitialLabels
            models.append(
                type(
                    name,
                    (Model,),
                    {
                        "__module__": __name__,
                        "id": fields.IntField(primary_key=True, generated=False),
                        "status": NativeEnumField(labels, type_name="shared_status", type_schema="shared types"),
                        "Meta": type("Meta", (), {"app": app, "table": f"prerequisite_{app}"}),
                    },
                )
            )
        return State.from_models(models, default_connections=aliases, environment=MigrationEnvironment())

    @staticmethod
    def get_definitions(migrations: list[Migration]) -> MigrationDefinitions:
        return MigrationDefinitions(
            {MigrationKey(migration.app_label, migration.name): migration for migration in migrations},
            frozenset(NativePrerequisiteSamples.apps),
            frozenset(),
        )

    @staticmethod
    def get_config(aliases: dict[str, str]) -> dict:
        return {
            app: {"default_connection": aliases[app], "migrations": f"tests._native_{app}"}
            for app in NativePrerequisiteSamples.apps
        }

    @staticmethod
    async def get_changes(state: State, previous: list[Migration] | None = None) -> list[MigrationWriter]:
        return await MigrationAutodetector(
            state.apps,
            NativePrerequisiteSamples.get_config(state.default_connections),
            definitions=NativePrerequisiteSamples.get_definitions(previous or []),
        ).changes()

    @staticmethod
    def get_history(writers: list[MigrationWriter]) -> list[Migration]:
        return [
            Migration.from_source(writer.as_string(), name=writer.name, app_label=writer.app_label)
            for writer in writers
        ]
