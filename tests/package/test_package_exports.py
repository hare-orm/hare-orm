"""Every name a hare package exports resolves, once in its ``__all__``, and ``dir()`` of a package
exporting its names lazily lists them before they are imported - a package importing an optional library in
the environment installing its extra."""

import ast
import importlib
from pathlib import Path

import pytest

import hare
from tests.utils.package_requirements import PackageRequirements

pytestmark = pytest.mark.database_independent


def get_exporting_packages() -> list[str]:
    """The packages of hare declaring ``__all__`` - found by reading their ``__init__.py``, not importing it.

    Returns:
        Their dotted names.
    """
    hare_directory = Path(hare.__file__).parent
    names = []
    for init_path in sorted(hare_directory.rglob("__init__.py")):
        tree = ast.parse(init_path.read_text(encoding="utf-8"))
        if any(
            isinstance(statement, (ast.Assign, ast.AnnAssign))
            and any(
                isinstance(target, ast.Name) and target.id == "__all__"
                for target in (statement.targets if isinstance(statement, ast.Assign) else [statement.target])
            )
            for statement in tree.body
        ):
            relative_parts = init_path.parent.relative_to(hare_directory.parent).parts
            names.append(".".join(relative_parts))
    return names


@pytest.mark.parametrize(
    "package_name",
    [
        pytest.param(name, marks=[pytest.mark.requires(*PackageRequirements.get(name))])
        if PackageRequirements.get(name)
        else name
        for name in get_exporting_packages()
    ],
)
def test_a_package_exports_each_name_once_and_lists_it(package_name):
    package = importlib.import_module(package_name)

    assert len(set(package.__all__)) == len(package.__all__)
    for name in package.__all__:
        getattr(package, name)
    # A lazily exported name was missing from dir() until something imported it.
    assert set(package.__all__) <= set(dir(package))
    with pytest.raises(AttributeError):
        package.NoSuchName  # noqa: B018
