"""Extra x entry point matrix: in whatever environment the suite runs, every module of hare the environment can
import imports, and the dialects and driver schemes load."""

import importlib

import pytest

from hare.dialects.dialect_registry import DialectRegistry
from tests.contracts.constants import ALWAYS_AVAILABLE_URL_SCHEMES
from tests.contracts.hare_registry import HareRegistry
from tests.utils.package_requirements import PackageRequirements

pytestmark = pytest.mark.database_independent


@pytest.mark.parametrize(
    "module_name",
    [
        pytest.param(name, marks=pytest.mark.requires(*PackageRequirements.get(name)))
        for name in HareRegistry.get_module_names()
        if HareRegistry.is_installed(name)
    ],
)
def test_a_module_the_environment_can_import_imports(module_name):
    importlib.import_module(module_name)


def test_the_dialects_and_drivers_load():
    DialectRegistry.load()
    schemes = DialectRegistry.get_url_schemes()
    assert set(ALWAYS_AVAILABLE_URL_SCHEMES) <= set(schemes)
    for scheme in schemes:
        assert DialectRegistry.get_driver_for_url_scheme(scheme).dialect is not None
