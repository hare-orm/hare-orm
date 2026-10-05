"""Code with one branch per built-in dialect raises for any other dialect instead of silently
rendering or doing what one of the built-in ones needs."""

import types

import pytest

from hare.dialects.base.dialect import Dialect
from hare.dialects.base.renderers import TermRenderers
from hare.exceptions import UnSupportedError
from hare.models.deletion.cascade.deletion_graph import DeletionGraph
from hare.models.deletion.cascade.protect_constraint_deferral import ProtectConstraintDeferral
from hare.sql.constants import DEFAULT_SQL_CONTEXT
from hare.sql.terms.criteria import JSONAttributeCriterion
from hare.sql.terms.field import Field
from tests.shared_models.testmodels import TransitiveCascadeGrandparent


class UnknownDialect(Dialect):
    name = "unknown"
    otel_system_name = "other_sql"

    def build_renderers(self) -> TermRenderers:
        return TermRenderers(self)


UNKNOWN_DIALECT_CLIENT = types.SimpleNamespace(connection_alias="unknown_dialect", dialect=UnknownDialect())


@pytest.mark.database_independent
def test_json_path_access_on_an_unknown_dialect_raises():
    criterion = JSONAttributeCriterion(Field("data"), ["a"])

    with pytest.raises(UnSupportedError, match="unknown"):
        criterion.get_sql(DEFAULT_SQL_CONTEXT.copy(dialect=UnknownDialect()))


@pytest.mark.asyncio
async def test_protect_deferral_on_an_unknown_dialect_raises(db_truncate):
    assert DeletionGraph.has_transitive_protect(TransitiveCascadeGrandparent)

    with pytest.raises(UnSupportedError, match="unknown"):
        async with ProtectConstraintDeferral.defer(TransitiveCascadeGrandparent, UNKNOWN_DIALECT_CLIENT):
            pass
