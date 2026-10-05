"""Models of the ClickHouse cluster tests - tables distributed over their shards, one every server keeps a
copy of, and one keeping the rows written through its server."""

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


class Click(Model):
    """A click of a visit - its rows spread over the shards by the hash of its key too."""

    id = fields.BigIntField(primary_key=True, generated=False)
    visit = fields.ForeignKeyField("models.Visit", related_name="clicks")

    class Meta:
        table_options = [
            ClickhouseTableOptions(distributed_over="click_local", sharding_key=RawSQLTerm("intHash64(id)"))
        ]


class Label(Model):
    """A label of visits - its rows, and its links to the visits, on the server they are written through."""

    name = fields.CharField(max_length=20, primary_key=True)
    visits = fields.ManyToManyField("models.Visit", related_name="labels")
