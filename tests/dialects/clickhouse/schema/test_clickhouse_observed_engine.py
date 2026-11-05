"""inspectdb wrote a Distributed engine with the name of the inspected database - a model generated from it
created, in another database, a Distributed table over the local table of the first one."""

import pytest

from hare.dialects.clickhouse.constants import CLICKHOUSE_DIALECT
from hare.dialects.clickhouse.introspection.clickhouse_catalog_engine import ClickhouseCatalogEngine
from hare.models import Model

pytestmark = pytest.mark.database_independent


def test_a_distributed_engine_over_its_own_database_names_it_current():
    def get_engine(database, sql):
        engine = ClickhouseCatalogEngine.get(sql, database)
        return engine if isinstance(engine, str) else engine.get_sql(Model, CLICKHOUSE_DIALECT)

    assert (
        get_engine("analytics", "Distributed('cluster', 'analytics', 'visit_local', intHash64(id))")
        == "Distributed('cluster', currentDatabase(), 'visit_local', intHash64(id))"
    )
    # Another database is named as it is, and an engine of another name is left alone.
    assert get_engine("analytics", "Distributed('cluster', 'other', 'visit_local')") == (
        "Distributed('cluster', 'other', 'visit_local')"
    )
    assert get_engine("analytics", "MergeTree") == "MergeTree"
