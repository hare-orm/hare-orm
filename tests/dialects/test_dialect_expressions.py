"""The functions of one database its dialect registers - another database's query refuses one before any SQL,
and no function of a dialect package goes unregistered."""

import importlib
import inspect
from pathlib import Path

import pytest

from hare.contrib.test import capture_queries
from hare.dialects.clickhouse.clickhouse_dialect import ClickhouseDialect
from hare.dialects.clickhouse.functions import GroupArray
from hare.dialects.postgresql.functions import JSONBAgg
from hare.dialects.postgresql.postgresql_dialect import PostgresqlDialect
from hare.exceptions import UnSupportedError
from hare.query.expressions.dialect_expressions import DialectExpressions
from hare.query.expressions.expression import Expression
from tests.shared_models.testmodels import Book
from tests.utils.database_under_test import DatabaseUnderTest

HARE_DIRECTORY = Path(__file__).resolve().parents[2] / "hare"
#: The dialect class of each built-in dialect package declaring functions of its own database.
DIALECT_CLASSES_BY_PACKAGE = {"postgresql": PostgresqlDialect, "clickhouse": ClickhouseDialect}


def get_expression_classes(package: str) -> list[type]:
    expression_classes = []
    for path in sorted((HARE_DIRECTORY / "dialects" / package).rglob("*.py")):
        module_name = ".".join(path.relative_to(HARE_DIRECTORY.parent).with_suffix("").parts).removesuffix(".__init__")
        try:
            module = importlib.import_module(module_name)
        except ImportError:  # a driver package not installed
            continue
        expression_classes.extend(
            value
            for value in vars(module).values()
            if inspect.isclass(value) and issubclass(value, Expression) and value.__module__ == module.__name__
        )
    return expression_classes


@pytest.mark.database_independent
@pytest.mark.parametrize("package", sorted(DIALECT_CLASSES_BY_PACKAGE))
def test_every_expression_of_a_dialect_package_is_registered_by_its_dialect(package):
    """An expression of one database left unregistered would reach another database's SQL."""
    dialect_class = DIALECT_CLASSES_BY_PACKAGE[package]
    # Imported first - a package registers its functions on import.
    expression_classes = get_expression_classes(package)
    registered_classes = [
        expression_class
        for expression_class, dialect_classes in DialectExpressions.registered.items()
        if dialect_class in dialect_classes
    ]
    unregistered = [
        expression_class.__qualname__
        for expression_class in expression_classes
        if dialect_class not in (DialectExpressions.get_dialect_classes(expression_class) or ())
        # A base of registered functions - its subclasses are the functions.
        and not any(issubclass(registered, expression_class) for registered in registered_classes)
    ]
    assert unregistered == []
    assert get_expression_classes("sqlite") == []


@pytest.mark.asyncio
async def test_a_function_of_another_database_is_refused_before_any_sql(db):
    """JSONB_AGG went to SQLite and ClickHouse, groupArray to SQLite and PostgreSQL - the database's own error
    instead of hare's refusal."""
    dialect = DatabaseUnderTest.get_dialect()
    foreign_functions = [
        function_class
        for function_class, dialect_class in ((JSONBAgg, PostgresqlDialect), (GroupArray, ClickhouseDialect))
        if not isinstance(dialect, dialect_class)
    ]
    assert foreign_functions
    for function_class in foreign_functions:
        async with capture_queries() as counter:
            with pytest.raises(UnSupportedError, match=rf"{function_class.__name__}\(\) is a function of the"):
                await Book.objects.values("author_id").annotate(names=function_class("name"))
        assert counter.count == 0
