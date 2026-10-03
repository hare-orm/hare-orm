"""Code with one branch per built-in dialect raises for any other dialect instead of silently
rendering or doing what one of the built-in ones needs."""

import types

import pytest

from hare.dialects.base.dialect import Dialect
from hare.dialects.base.renderers import TermRenderers
from hare.exceptions import UnSupportedError
from hare.models.deletion.deletion_graph import DeletionGraph
from hare.models.deletion.protect_constraint_deferral import ProtectConstraintDeferral
from hare.sql.context import DEFAULT_SQL_CONTEXT
from hare.sql.terms.criteria import JSONAttributeCriterion
from hare.sql.terms.field import Field
from tests.testmodels import TransitiveCascadeGrandparent


class ColumnarDialect(Dialect):
    name = "columnar"
    otel_system_name = "other_sql"

    def build_renderers(self) -> TermRenderers:
        return TermRenderers()


UNKNOWN_DIALECT_CLIENT = types.SimpleNamespace(connection_name="unknown_dialect", dialect=ColumnarDialect())


def test_json_path_access_on_an_unknown_dialect_raises():
    criterion = JSONAttributeCriterion(Field("data"), ["a"])

    with pytest.raises(UnSupportedError, match="columnar"):
        criterion.get_sql(DEFAULT_SQL_CONTEXT.copy(dialect=ColumnarDialect()))


@pytest.mark.asyncio
async def test_protect_deferral_on_an_unknown_dialect_raises(db_truncate):
    assert DeletionGraph.has_transitive_protect(TransitiveCascadeGrandparent)

    with pytest.raises(UnSupportedError, match="columnar"):
        async with ProtectConstraintDeferral.defer(TransitiveCascadeGrandparent, UNKNOWN_DIALECT_CLIENT):
            pass
