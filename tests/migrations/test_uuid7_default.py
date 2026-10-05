"""A version 7 UUID made in Python - ``default=uuid.uuid7`` (Python 3.14) or the function of a
library (``uuid_utils.uuid7``, written in C): a migration writes the function by its import path and
reads the same function back."""

import time
import uuid

import pytest

from hare.migrations.writer import ImportManager, MigrationWriter
from tests.migrations.test_round_trip import RoundTripProject, database_url, round_trip_project  # noqa: F401

requires_uuid7 = pytest.mark.skipif(not hasattr(uuid, "uuid7"), reason="uuid.uuid7 comes with Python 3.14")

UUID7_MODELS = """
import uuid

from hare import fields
from hare.models import Model


class Event(Model):
    id = fields.UUIDField(primary_key=True, default=uuid.uuid7)
    name = fields.CharField(max_length=50)
"""


def read_back(function):
    """The object a migration file gets for ``function`` - its imports run, its expression evaluated."""
    imports = ImportManager()
    rendered = MigrationWriter.render_value(function, imports)
    namespace: dict = {}
    exec("\n".join(imports.render()), namespace)  # noqa: S102
    return eval(rendered, namespace)  # noqa: S307


@requires_uuid7
def test_uuid7_is_written_by_its_import_path():
    assert read_back(uuid.uuid7) is uuid.uuid7


def test_a_builtin_function_is_written_by_its_import_path():
    # A library's uuid7 written in C is a builtin function, as time.time_ns is.
    assert read_back(time.time_ns) is time.time_ns


@requires_uuid7
@pytest.mark.asyncio
async def test_a_uuid7_default_round_trips_through_a_written_migration(
    round_trip_project: RoundTripProject,  # noqa: F811
) -> None:
    round_trip_project.write_models(UUID7_MODELS)
    await round_trip_project.make_and_migrate()

    await round_trip_project.assert_nothing_left_to_do()
    (migration_file_name,) = round_trip_project.migration_file_names()
    create_model_operation = round_trip_project.load_migration_operations(migration_file_name)[0]
    assert dict(create_model_operation.fields)["id"].default is uuid.uuid7
