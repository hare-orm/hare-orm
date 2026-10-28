"""Documentation x code matrix: every name the python blocks of the documentation import from hare exists - the
module imports and the name is in it. A name documented and missing from the code fails here."""

import importlib

import pytest

from tests.contracts.documentation.documented_imports import DocumentedImports
from tests.contracts.hare_registry import HareRegistry

pytestmark = pytest.mark.database_independent

DOCUMENTED_IMPORTS = [
    documented for documented in DocumentedImports.get_all() if HareRegistry.is_installed(documented.module)
]


def test_the_documentation_has_imports_to_check():
    assert len(DocumentedImports.get_all()) > 100


@pytest.mark.parametrize(
    "documented", DOCUMENTED_IMPORTS, ids=[documented.identifier for documented in DOCUMENTED_IMPORTS]
)
def test_a_documented_import_exists(documented):
    module = importlib.import_module(documented.module)
    if documented.name is None or documented.name == "*":
        return
    if hasattr(module, documented.name):
        return
    try:
        importlib.import_module(f"{documented.module}.{documented.name}")
    except ModuleNotFoundError:
        pytest.fail(f"{documented.module} has no {documented.name}, documented in {', '.join(documented.files)}")
