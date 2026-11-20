"""Migration imports preserve values when names of different imports overlap."""

import datetime
import functools
import sys
import types
import typing
import uuid

import pytest

from hare import fields
from hare.ddl.constraints import UniqueConstraint
from hare.ddl.indexes import Index, OrderedIndexKey
from hare.ddl.raw_sql_term import RawSQLTerm
from hare.declarations.declaration_value_serializer import DeclarationValueSerializer
from hare.declarations.import_manager import ImportManager
from hare.migrations.operations import RunSQL
from hare.migrations.writer import MigrationWriter
from hare.sql.order import Order

pytestmark = pytest.mark.database_independent


class Outer:
    class Inner:
        def deconstruct(self):
            return f"{__name__}.Outer.Inner", [], {}


@pytest.mark.parametrize("nested", [False, True])
@pytest.mark.parametrize("explicit_path", [False, True])
def test_custom_operation_keeps_its_import_path_and_builtin_name(monkeypatch, nested, explicit_path):
    module = types.ModuleType("custom_migration_operations")
    source = "from hare.migrations.operations import RunSQL as BaseRunSQL\nclass RunSQL(BaseRunSQL):\n"
    source += (
        "    def deconstruct(self):\n        return __name__ + '.RunSQL', [], {'sql': self.sql}\n"
        if explicit_path
        else "    pass\n"
    )
    exec(compile(source, "custom_operations", "exec", dont_inherit=True), module.__dict__)  # noqa: S102
    exec(
        "from hare.migrations.operations import SeparateDatabaseAndState as BaseSeparate\n"
        "class SeparateDatabaseAndState(BaseSeparate):\n    pass\n",
        module.__dict__,
    )  # noqa: S102
    monkeypatch.setitem(sys.modules, module.__name__, module)
    custom = module.RunSQL(sql=["SELECT 1", "SELECT 2"])
    declarations = [RunSQL("SELECT 3"), custom]
    if nested:
        declarations = [module.SeparateDatabaseAndState(database_operations=declarations)]
    source = MigrationWriter("0001_custom", "books", declarations).as_string()
    namespace = {}
    exec(compile(source, "generated_migration", "exec", dont_inherit=True), namespace)  # noqa: S102
    restored = namespace["Migration"].operations
    if nested:
        assert type(restored[0]) is module.SeparateDatabaseAndState
        restored = restored[0].database_operations
    assert type(restored[0]) is RunSQL
    assert type(restored[1]) is module.RunSQL
    assert restored[1].sql == custom.sql


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("uuid", uuid.UUID(int=7)),
        ("datetime", datetime.datetime(2026, 10, 29)),
        ("datetime", datetime.timedelta(days=2)),
        ("typing", typing.Literal["example"]),
        ("functools", functools.partial(int, "7")),
        ("fields", fields.IntField()),
        ("Index", Index(fields=("id",))),
        ("UniqueConstraint", UniqueConstraint(fields=("id",))),
        ("int", int),
        ("list", list[int]),
    ],
)
def test_custom_names_do_not_shadow_other_imports(monkeypatch, name, value, reverse):
    module = types.ModuleType("migration_name_probe")
    exec(f"def {name}():\n    return 11\n", module.__dict__)  # noqa: S102
    monkeypatch.setitem(sys.modules, module.__name__, module)
    function = getattr(module, name)
    values = (value, function) if reverse else (function, value)
    imports = ImportManager()
    expression = DeclarationValueSerializer.render_value(values, imports)
    namespace = {}
    exec("\n".join(imports.render()), namespace)  # noqa: S102
    rebuilt = eval(expression, namespace)  # noqa: S307
    function_index, value_index = (1, 0) if reverse else (0, 1)
    assert rebuilt[function_index] is function
    assert type(rebuilt[value_index]) is type(value)
    if not hasattr(value, "deconstruct"):
        if isinstance(value, functools.partial):
            assert rebuilt[value_index]() == 7
        else:
            assert rebuilt[value_index] == value


@pytest.mark.parametrize("reverse", [False, True])
def test_grouped_and_regular_imports_share_their_reference(reverse):
    imports = ImportManager()
    key = OrderedIndexKey(RawSQLTerm("id"), Order.DESC)
    renderers = (
        lambda: DeclarationValueSerializer.render_call(
            "hare.ddl.indexes.OrderedIndexKey", [key.term, key.order], {}, imports
        ),
        lambda: DeclarationValueSerializer.render_value(key, imports),
    )
    expressions = [renderer() for renderer in (reversed(renderers) if reverse else renderers)]
    namespace = {}
    exec("\n".join(imports.render()), namespace)  # noqa: S102
    for expression in expressions:
        rebuilt = eval(expression, namespace)  # noqa: S307
        assert isinstance(rebuilt, OrderedIndexKey)
        assert rebuilt.order == Order.DESC


def test_dotted_modules_keep_their_reference_when_the_root_is_taken(monkeypatch):
    package = types.ModuleType("migration_import_package")
    package.__path__ = []
    child = types.ModuleType("migration_import_package.child")
    package.child = child
    monkeypatch.setitem(sys.modules, package.__name__, package)
    monkeypatch.setitem(sys.modules, child.__name__, child)
    source = types.ModuleType("migration_import_source")
    original_value = object()
    setattr(source, package.__name__, original_value)
    monkeypatch.setitem(sys.modules, source.__name__, source)
    imports = ImportManager()
    original_reference = imports.add_from(source.__name__, package.__name__)
    reference = imports.add_module(child.__name__)
    assert reference == imports.add_module(child.__name__)
    namespace = {}
    exec("\n".join(imports.render()), namespace)  # noqa: S102
    assert eval(reference, namespace) is child  # noqa: S307
    assert eval(original_reference, namespace) is original_value  # noqa: S307


def test_deconstructed_nested_class_is_imported_through_its_module():
    imports = ImportManager()
    expression = DeclarationValueSerializer.render_value(Outer.Inner(), imports)
    namespace = {}
    exec("\n".join(imports.render()), namespace)  # noqa: S102
    assert isinstance(eval(expression, namespace), Outer.Inner)  # noqa: S307
    assert DeclarationValueSerializer.get_callable(f"{__name__}.Outer.Inner") is Outer.Inner
