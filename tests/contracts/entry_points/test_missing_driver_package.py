"""Extra x entry point matrix, the connection column: a DB_URL of a driver whose package is missing is refused
with a ConfigurationError saying what to install - never an ImportError out of the driver. The package is made
missing by hiding it from the import system, so the check runs in every environment."""

import sys

import pytest

from hare.contrib.test.isolated_contexts import hare_test_context
from hare.exceptions import ConfigurationError
from tests.contracts.constants import DRIVER_PACKAGES_BY_SCHEME

pytestmark = pytest.mark.database_independent


@pytest.mark.asyncio
@pytest.mark.parametrize(("scheme", "package"), sorted(DRIVER_PACKAGES_BY_SCHEME.items()))
async def test_a_url_of_a_driver_without_its_package_is_a_configuration_error(monkeypatch, scheme, package):
    for module_name in [name for name in sys.modules if name == package or name.startswith(f"{package}.")]:
        monkeypatch.delitem(sys.modules, module_name)
    for module_name in [name for name in sys.modules if name.startswith("hare.dialects.") and ".drivers." in name]:
        if package.replace("_", "-") in module_name.replace("_", "-"):
            monkeypatch.delitem(sys.modules, module_name)
    monkeypatch.setitem(sys.modules, package, None)
    with pytest.raises(ConfigurationError, match=package.replace("_", "-")):
        async with hare_test_context(
            modules=["tests.contracts.models_anchor"],
            db_url=f"{scheme}://user:password@127.0.0.1:1/test_{{}}",
            app_label="contract_anchor",
            connection_label="models",
            _generate_schemas=False,
            _create_db=False,
        ):
            pass
