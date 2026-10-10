"""Models of the ClickHouse lightweight update tests - tables changed by lightweight UPDATEs, which a
server of ClickHouse 25.7 runs."""

from hare import fields
from hare.dialects.clickhouse.clickhouse_table_options import ClickhouseTableOptions
from hare.models import Model


class Ledger(Model):
    """A ledger of an owner - its balance changed by lightweight UPDATEs."""

    id = fields.BigIntField(primary_key=True, generated=False)
    owner = fields.CharField(max_length=20)
    balance = fields.IntField(default=0)

    class Meta:
        table_options = [ClickhouseTableOptions(lightweight_updates=True)]
