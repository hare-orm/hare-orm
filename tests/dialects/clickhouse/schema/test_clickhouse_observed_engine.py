"""inspectdb wrote a Distributed engine with the name of the inspected database - a model generated from it
created, in another database, a Distributed table over the local table of the first one."""

from types import SimpleNamespace

import pytest

from hare.dialects.clickhouse.constants import CLICKHOUSE_DIALECT
from hare.dialects.clickhouse.introspection.clickhouse_observed_table_options import ClickhouseObservedTableOptions

pytestmark = pytest.mark.database_independent


def test_a_distributed_engine_over_its_own_database_names_it_current():
    connection = SimpleNamespace(dialect=CLICKHOUSE_DIALECT, database="analytics")
    get_engine = ClickhouseObservedTableOptions.get_engine_of_any_database
    assert (
        get_engine(connection, "Distributed('cluster', 'analytics', 'visit_local', intHash64(id))")
        == "Distributed('cluster', currentDatabase(), 'visit_local', intHash64(id))"
    )
    # Another database is named as it is, and an engine of another name is left alone.
    assert get_engine(connection, "Distributed('cluster', 'other', 'visit_local')") == (
        "Distributed('cluster', 'other', 'visit_local')"
    )
    assert get_engine(connection, "MergeTree") == "MergeTree"
