"""Every name a hare package exports resolves, once in its ``__all__``, and ``dir()`` of a package
exporting its names lazily lists them before they are imported."""

import importlib
import pkgutil

import pytest

import hare

pytestmark = pytest.mark.database_independent

#: Packages importing a library hare doesn't require.
OPTIONAL_PACKAGE_PARTS = ("frameworks", "taskiq", "opentelemetry", "mypy", "pytest")


def get_exporting_packages() -> list[str]:
    names = []
    for module_info in pkgutil.walk_packages(hare.__path__, "hare."):
        if module_info.ispkg and not any(part in module_info.name for part in OPTIONAL_PACKAGE_PARTS):
            if hasattr(importlib.import_module(module_info.name), "__all__"):
                names.append(module_info.name)
    return names


@pytest.mark.parametrize("package_name", get_exporting_packages())
def test_a_package_exports_each_name_once_and_lists_it(package_name):
    package = importlib.import_module(package_name)

    assert len(set(package.__all__)) == len(package.__all__)
    for name in package.__all__:
        getattr(package, name)
    # A lazily exported name was missing from dir() until something imported it.
    assert set(package.__all__) <= set(dir(package))
    with pytest.raises(AttributeError):
        package.NoSuchName  # noqa: B018
