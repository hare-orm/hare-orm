"""What every registered dialect and driver provides - hare's own and the columnar test dialect
alike: its name, features, registries, schema editor and deterministic SQL."""

import os
import subprocess
import sys

import pytest

from hare.dialects.base.clauses.query_clauses import QueryClauses
from hare.dialects.base.client import DatabaseClient
from hare.dialects.base.connection.driver import Driver
from hare.dialects.base.dialect import Dialect
from hare.dialects.base.features import Features
from hare.dialects.base.literals.sql_literals import SqlLiterals
from hare.dialects.base.lookups.filter_operators import FilterOperators
from hare.dialects.base.migration_safety import MigrationSafetyRule, MigrationSafetyRules
from hare.dialects.base.parameters.sql_parameters import SqlParameters
from hare.dialects.base.renderers import TermRenderers
from hare.dialects.base.schema.base_schema_editor import BaseSchemaEditor
from hare.dialects.base.transactions.transaction_statements import TransactionStatements
from hare.dialects.base.types import TypeRegistry
from hare.dialects.dialect_registry import DialectRegistry
from hare.inspectdb.introspection import SchemaIntrospector
from hare.sql.terms.field import Field
from hare.sql.terms.parameters.parameter import Parameter

DRIVERS = DialectRegistry.get_drivers()
DIALECTS = DialectRegistry.get_dialects()


@pytest.mark.parametrize("driver", DRIVERS, ids=[driver.name for driver in DRIVERS])
def test_a_driver_names_its_dialect_schemes_and_clients(driver: Driver):
    assert driver.name
    assert driver.url_schemes
    assert DialectRegistry.get_dialect(driver.dialect.name) is driver.dialect
    for scheme in driver.url_schemes:
        assert DialectRegistry.get_driver_for_url_scheme(scheme) is driver
    for client_class in driver.get_client_classes():
        assert issubclass(client_class, DatabaseClient)
        assert client_class.dialect is driver.dialect
        assert client_class.get_driver() is driver
        assert isinstance(client_class.features, Features)
        # DDL can only roll back inside a transaction.
        assert client_class.features.supports_transactions or not client_class.features.can_rollback_ddl
        assert client_class.query_class.SQL_CONTEXT.dialect is driver.dialect


@pytest.mark.parametrize("dialect", DIALECTS, ids=[dialect.name for dialect in DIALECTS])
def test_a_dialect_builds_every_part_of_its_own(dialect: Dialect):
    """Every part a dialect is assembled from is built for that dialect - what it writes is the
    dialect's, never another's."""
    parts = (
        (dialect.literals, SqlLiterals),
        (dialect.parameters, SqlParameters),
        (dialect.renderers, TermRenderers),
        (dialect.clauses, QueryClauses),
        (dialect.transactions, TransactionStatements),
        (dialect.filter_operators, FilterOperators),
        (dialect.migration_safety_rules, MigrationSafetyRules),
    )
    for part, part_class in parts:
        assert isinstance(part, part_class)
        assert part.dialect is dialect
    assert isinstance(dialect.features, Features)
    rule_codes = [rule.code for rule in dialect.migration_safety_rules.rules]
    assert rule_codes
    assert len(rule_codes) == len(set(rule_codes))
    assert all(isinstance(rule, MigrationSafetyRule) for rule in dialect.migration_safety_rules.rules)


@pytest.mark.parametrize("dialect", DIALECTS, ids=[dialect.name for dialect in DIALECTS])
def test_a_dialect_builds_every_registry(dialect: Dialect):
    assert dialect.name
    assert dialect.otel_system_name
    assert isinstance(dialect.types, TypeRegistry)
    assert isinstance(dialect.filter_operators, FilterOperators)
    assert isinstance(dialect.renderers, TermRenderers)
    assert issubclass(dialect.schema_editor_class, BaseSchemaEditor)
    introspector_class = dialect.introspector_class
    assert introspector_class is None or issubclass(introspector_class, SchemaIntrospector)
    assert dialect.parameters.get_placeholder(1)
    if "{}" in dialect.parameters.placeholder_template:
        assert dialect.parameters.get_placeholder(1) != dialect.parameters.get_placeholder(2)
    assert list(dialect.features.isolation_levels) == sorted(
        dialect.features.isolation_levels, key=list(type(dialect.features.isolation_levels[0])).index
    )


@pytest.mark.parametrize("driver", DRIVERS, ids=[driver.name for driver in DRIVERS])
def test_the_same_query_renders_the_same_sql(driver: Driver):
    client_classes = driver.get_client_classes()
    if not client_classes:
        pytest.skip(f"{driver.name}: the driver's library isn't installed")
    query_class = client_classes[0].query_class

    def render() -> str:
        return (
            query_class.from_("event")
            .select("id", "name")
            .where((Field("name") == Parameter(1)) & (Field("id") > Parameter(2)))
            .orderby("name")
            .get_sql()
        )

    assert render() == render()


def test_a_select_lists_columns_the_same_in_every_process():
    script = (
        "import asyncio\n"
        "from hare.contrib.test import hare_test_context\n"
        "async def main():\n"
        "    async with hare_test_context(['tests.testmodels'], db_url='sqlite+aiosqlite://:memory:'):\n"
        "        from tests.testmodels import Event\n"
        "        print(Event.objects.all().sql())\n"
        "asyncio.run(main())\n"
    )
    # The processes run at once.
    processes = [
        subprocess.Popen(
            [sys.executable, "-c", script],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env={**os.environ, "PYTHONHASHSEED": hash_seed},
            stdin=subprocess.DEVNULL,
        )
        for hash_seed in ("1", "2", "3")
    ]
    rendered = set()
    for process in processes:
        stdout, stderr = process.communicate(timeout=120)
        assert process.returncode == 0, stderr
        rendered.add(stdout.strip())
    assert len(rendered) == 1
