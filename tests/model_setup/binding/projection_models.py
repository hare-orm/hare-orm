from hare import Model, fields
from hare.dialects.clickhouse.clickhouse_table_options import ClickhouseTableOptions
from tests.utils.compatible_projection import CompatibleProjection


class ProjectionRecord(Model):
    score = fields.IntField()

    class Meta:
        table_options = (ClickhouseTableOptions(projections=(CompatibleProjection(),)),)
