import sys
from types import ModuleType

import pytest

from hare.core.config import HareConfig
from hare.core.hare_context import HareContext
from hare.query.scopes.tenants.model_tenants import ModelTenants
from tests.dialects.postgresql.conftest import skip_if_not_postgres
from tests.dialects.postgresql.tenancy.models_tenant_row_level_security import RlsLabel, RlsNote
from tests.dialects.postgresql.tenancy.models_tenant_schema_model_scopes import ScopeMemo
from tests.utils.database_under_test import DatabaseUnderTest


@pytest.mark.asyncio
@pytest.mark.parametrize("scope_type", ("schema", "policy"))
async def test_a_scope_keeps_the_tenants_of_each_context_separate(monkeypatch, scope_type):
    skip_if_not_postgres()
    contexts = []
    try:
        for index, models in enumerate(((ScopeMemo, RlsNote), (RlsLabel,))):
            module = ModuleType(f"tenant_context_{index}")
            module.__models__ = models
            monkeypatch.setitem(sys.modules, module.__name__, module)
            context = HareContext()
            contexts.append(context)
            url = DatabaseUnderTest.get_url()
            url += f"{'&' if '?' in url else '?'}tenant_schema_template=tenant_%7Btenant%7D"
            with context:
                await context.init(HareConfig.from_db_url(url, {"models": [module]}), connect=False)
        scope = ModelTenants({ScopeMemo: "acme", RlsNote: 1, RlsLabel: "beta"})
        expected = ("acme", None) if scope_type == "schema" else ({"rls_note": 1}, {"rls_label": "beta"})
        read = scope.get_schema_tenant if scope_type == "schema" else scope.get_policy_tenants
        for index in (0, 1, 0, 1):
            with contexts[index]:
                assert read("default") == expected[index]
    finally:
        for context in contexts:
            await context.close_connections()
