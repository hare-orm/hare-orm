"""Values are converted between Python and the database only through the type registry of the
dialect the query runs on - a direct field conversion call would skip the dialect's own hooks."""

import ast
from pathlib import Path

import hare

HARE_ROOT = Path(hare.__file__).parent
CONVERSION_METHODS = {"to_db_value", "to_lookup_value", "from_db_value"}
#: Modules defining fields and type registries - a field converts its own values there.
FIELD_DEFINITION_MODULES = {
    "fields/composite_primary_key.py",
    "fields/data.py",
    "fields/generated.py",
    "fields/swappable.py",
    "dialects/postgresql/types.py",
    "dialects/sqlite/types.py",
}
FIELD_DEFINITION_PACKAGES = (
    "dialects/postgresql/fields/",
    "fields/base/",
    "fields/db_defaults/",
    "fields/encrypted/",
    "dialects/base/types/",
)
EXCLUDED_PACKAGES = ("contrib/admin/", "contrib/ui/", "contrib/site/")


def get_direct_conversion_calls() -> list[str]:
    calls = []
    for path in sorted(HARE_ROOT.rglob("*.py")):
        module = path.relative_to(HARE_ROOT).as_posix()
        if module in FIELD_DEFINITION_MODULES or module.startswith(FIELD_DEFINITION_PACKAGES + EXCLUDED_PACKAGES):
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in CONVERSION_METHODS
            ):
                continue
            receiver = node.func.value
            is_own_method = (isinstance(receiver, ast.Name) and receiver.id == "self") or (
                isinstance(receiver, ast.Call) and isinstance(receiver.func, ast.Name) and receiver.func.id == "super"
            )
            if not is_own_method:
                calls.append(f"{module}:{node.lineno} {ast.unparse(node.func)}")
    return calls


def test_values_are_converted_through_the_dialect_type_registry():
    assert get_direct_conversion_calls() == []
