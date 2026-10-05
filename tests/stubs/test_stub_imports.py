"""``hare stubs``: the names a stub imports and declares - an import or a declaration named like another
name of the module takes a free one."""

from __future__ import annotations

import sys
import types

import pytest

from hare.stubs.stub_imports import StubImports

pytestmark = pytest.mark.database_independent


def test_a_class_named_like_one_of_the_stubs_own_is_imported_under_an_alias(monkeypatch):
    shop_module = types.ModuleType("stub_clash_shop")
    stock_module = types.ModuleType("stub_clash_stock")
    other_module = types.ModuleType("stub_clash_other")
    for module in (shop_module, stock_module, other_module):
        module.Item = type("Item", (), {"__module__": module.__name__})
        monkeypatch.setitem(sys.modules, module.__name__, module)
    stock_module.Shared = shop_module.Item
    imports = StubImports("stub_clash_stock")

    assert imports.add_class(shop_module.Item) == "stub_clash_shop_Item"
    assert imports.add_class(stock_module.Item) == "Item"
    assert imports.add_class(other_module.Item) == "stub_clash_other_Item"
    assert imports.add("typing", "Any") == "Any"
    assert imports.render() == [
        "from stub_clash_other import Item as stub_clash_other_Item",
        "from stub_clash_shop import Item as stub_clash_shop_Item",
        "from typing import Any",
    ]


def test_a_declaration_named_like_a_class_of_the_module_takes_a_free_name(monkeypatch):
    """A model named BookFilters was shadowed by the TypedDict of Book's filter keys."""
    module = types.ModuleType("stub_declarations")
    module.BookFilters = type("BookFilters", (), {"__module__": module.__name__})
    monkeypatch.setitem(sys.modules, module.__name__, module)
    imports = StubImports("stub_declarations")

    assert imports.declare("BookFilters") == "BookFilters_"
    assert imports.declare("BookFilters") == "BookFilters__"
    assert imports.declare("BookQuerySet") == "BookQuerySet"
    # An import named like a declaration is imported under an alias.
    assert imports.add("somewhere", "BookQuerySet") == "somewhere_BookQuerySet"
