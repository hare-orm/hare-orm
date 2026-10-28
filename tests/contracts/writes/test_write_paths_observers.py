"""Write contract matrix: every write path of the ORM against every observer of what it writes - the cell
of a path and an observer holds what the observer's table says of the path (observed, not applicable,
refused). Runs on the databases keeping what the matrix's models declare: unique names and foreign keys."""

import pytest

from tests.contracts.constants import CONTRACT_CONNECTION
from tests.contracts.writes.write_models import WriteModels
from tests.contracts.writes.write_observers import WRITE_OBSERVERS
from tests.contracts.writes.write_paths import WRITE_PATHS
from tests.contracts.writes.write_scenario import WriteScenario
from tests.utils.database_under_test import DatabaseUnderTest


def get_cells() -> list:
    """Every path with every observer, requiring the observer's extras."""
    return [
        pytest.param(
            observer_class,
            path,
            id=f"{observer_class.name}-{path.name}",
            marks=[pytest.mark.requires(*observer_class.requires)] if observer_class.requires else [],
        )
        for observer_class in WRITE_OBSERVERS
        for path in WRITE_PATHS
    ]


def skip_if_missing(features, names) -> None:
    """Skips the cell when the features lack one of the names."""
    missing_features = [name for name in names if not getattr(features, name)]
    if missing_features:
        pytest.skip(f"the cell needs {', '.join(missing_features)}")


@pytest.mark.asyncio
@pytest.mark.parametrize(("observer_class", "path"), get_cells())
async def test_a_write_path_does_what_an_observer_expects(contract_database, observer_class, path):
    needed = (*path.required_features, *observer_class.required_features)
    skip_if_missing(DatabaseUnderTest.get_dialect().features, needed)
    observer = observer_class()
    scenario = WriteScenario(contract_database, WriteModels.make(observer), observer)
    await scenario.prepare()
    # The features a server's version gives are known once its connection is open - MERGE from PostgreSQL 15.
    skip_if_missing(
        contract_database.connections.get(CONTRACT_CONNECTION).features,
        (*needed, *observer_class.path_required_features.get(path.name, ())),
    )
    if path.prepare is not None:
        with observer.get_scope():
            await path.prepare(scenario)
    await observer.check(scenario, path, observer.get_outcome(path))
