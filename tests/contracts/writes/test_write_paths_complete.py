"""The registries of the write matrix are complete: every method of hare that writes rows has a write path,
and every observer says what each path does to what it watches - a write method or a path added to hare fails
here until the matrix covers it."""

import pytest

from tests.contracts.constants import NOT_WRITE_METHODS
from tests.contracts.writes.enums import WriteOutcome
from tests.contracts.writes.write_methods import WriteMethods
from tests.contracts.writes.write_observers import WRITE_OBSERVERS
from tests.contracts.writes.write_paths import WRITE_PATHS

pytestmark = pytest.mark.database_independent


def get_path_methods() -> set[str]:
    """The methods the paths run - the part of a path's name before its variant."""
    return {path.name.split(",")[0] for path in WRITE_PATHS}


def test_every_write_method_has_a_path_or_writes_no_row():
    uncovered = WriteMethods.get_write_methods() - get_path_methods() - set(NOT_WRITE_METHODS)
    assert sorted(uncovered) == []


def test_every_path_and_no_write_method_names_an_existing_method():
    existing = WriteMethods.get_write_methods()
    assert sorted(get_path_methods() - existing) == []
    assert sorted(set(NOT_WRITE_METHODS) - existing) == []


def test_the_path_names_are_unique():
    names = [path.name for path in WRITE_PATHS]
    assert len(names) == len(set(names))


@pytest.mark.parametrize("observer_class", WRITE_OBSERVERS, ids=[observer.name for observer in WRITE_OBSERVERS])
def test_every_observer_says_what_every_path_does_to_it(observer_class):
    assert set(observer_class.outcomes) == {path.name for path in WRITE_PATHS}
    assert all(isinstance(outcome, WriteOutcome) for outcome in observer_class.outcomes.values())


def test_the_observer_names_are_unique():
    names = [observer.name for observer in WRITE_OBSERVERS]
    assert len(names) == len(set(names))
