"""Models of the ClickHouse cluster tests - a table distributed over its shards, and one every server
keeps a copy of."""

from hare import fields
from hare.ddl.raw_sql_term import RawSQLTerm
from hare.dialects.clickhouse.clickhouse_table_options import ClickhouseTableOptions
from hare.models import Model


class Visit(Model):
    """A visit of a site - its rows spread over the shards by the hash of its key."""

    id = fields.BigIntField(primary_key=True, generated=False)
    site = fields.CharField(max_length=20)
    duration = fields.IntField(default=0)

    class Meta:
        table_options = [
            ClickhouseTableOptions(distributed_over="visit_local", sharding_key=RawSQLTerm("intHash64(id)"))
        ]


class Site(Model):
    """A site - kept whole on every server of a shard."""

    name = fields.CharField(max_length=20, primary_key=True)
    owner = fields.CharField(max_length=50)

    class Meta:
        table_options = [ClickhouseTableOptions(engine="ReplicatedMergeTree")]
