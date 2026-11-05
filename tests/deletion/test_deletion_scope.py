"""Schema visits bind a separate transaction wrapper and restore task context on every exit."""

from unittest.mock import AsyncMock, patch

import pytest

from hare.contrib.test import requires_features
from hare.core.connections.connections import Connections
from hare.inspectdb.introspection.database_catalog import DatabaseCatalog
from hare.models.deletion.cascade.deletion_scope import DeletionScope
from hare.models.deletion.cascade.tenant_schema_visits import TenantSchemaVisits
from hare.models.tenancy.tenancy import Tenancy
from hare.query.scopes.row_scope_binding import RowScopeBinding
from hare.transactions.transactions import Transactions


@requires_features(supports_transactions=True, supports_savepoints=True)
@pytest.mark.asyncio
@pytest.mark.parametrize("fails", [False, True])
async def test_schema_visit_never_changes_parent_wrappers(db, fails):
    async with Transactions.atomic() as connection:
        original_schema = connection.tenant_schema
        original_tenant = Tenancy.current.get()
        original_binding = RowScopeBinding.current.get()
        original_connection = Connections.get(connection.connection_alias)
        parent = connection._parent
        parent_schema = parent.tenant_schema
        entered = AsyncMock(return_value="original search path")
        left = AsyncMock()
        with (
            patch.object(type(connection), "enter_tenant_schema", entered),
            patch.object(type(connection), "leave_tenant_schema", left),
            patch.object(DatabaseCatalog, "get_table_names", AsyncMock(return_value=["book"])),
        ):
            try:
                async with TenantSchemaVisits.visit(connection, "tenant_two", "two") as scope:
                    assert scope.connection is not connection
                    assert Connections.get(connection.connection_alias) is scope.connection
                    assert scope.connection.tenant_schema == "tenant_two"
                    assert connection.tenant_schema == original_schema
                    assert parent.tenant_schema == parent_schema
                    assert DeletionScope.current.get() is scope
                    assert Tenancy.current.get() == "two"
                    async with scope.connection._in_transaction() as nested:
                        assert scope.includes(nested)
                    assert not scope.includes(connection)
                    if fails:
                        raise RuntimeError("visit failed")
            except RuntimeError as error:
                assert fails and str(error) == "visit failed"
            assert connection.tenant_schema == original_schema
            assert parent.tenant_schema == parent_schema
            assert Connections.get(connection.connection_alias) is original_connection
            assert DeletionScope.current.get() is None
            assert RowScopeBinding.current.get() is original_binding
            assert Tenancy.current.get() == original_tenant
            entered.assert_awaited_once_with("tenant_two")
            if fails:
                left.assert_not_awaited()
            else:
                left.assert_awaited_once_with("original search path")
