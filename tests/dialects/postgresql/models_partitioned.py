"""Partitioned tables of each strategy, with foreign keys to and from them."""

import datetime

from hare import fields
from hare.dialects.postgresql.partitioning import (
    HashPartitioning,
    ListPartition,
    ListPartitioning,
    RangeBound,
    RangePartition,
    RangePartitioning,
)
from hare.dialects.postgresql.table_options import PostgresqlTableOptions
from hare.models import Model


class Reader(Model):
    id = fields.IntField(primary_key=True)
    name = fields.CharField(max_length=30)


class UnreadReport(Model):
    """Hash partitioned by a column of its composite primary key."""

    user_id = fields.BigIntField()
    report_id = fields.BigIntField()
    title = fields.CharField(max_length=50, default="")
    seen_count = fields.IntField(default=0)
    reader = fields.ForeignKeyField("models.Reader", related_name="unread_reports", null=True)
    pk = fields.CompositePrimaryKey("user_id", "report_id")

    class Meta:
        table_options = [
            PostgresqlTableOptions(
                partitioning=HashPartitioning(fields=("user_id",), partition_count=4),
                storage_parameters={"autovacuum_vacuum_scale_factor": 0.02},
            )
        ]


class ReportNote(Model):
    """References a partitioned table by its composite key."""

    id = fields.IntField(primary_key=True)
    report = fields.ForeignKeyField("models.UnreadReport", related_name="notes")
    text = fields.CharField(max_length=30, default="")


class RegionSale(Model):
    """List partitioned, with a default partition."""

    region = fields.CharField(max_length=10)
    number = fields.IntField()
    amount = fields.IntField(default=0)
    pk = fields.CompositePrimaryKey("region", "number")

    class Meta:
        table_options = [
            PostgresqlTableOptions(
                partitioning=ListPartitioning(
                    fields=("region",),
                    partitions=[ListPartition("west", values=["us", "ca"]), ListPartition("east", values=["jp"])],
                    default_partition="other",
                )
            )
        ]


class DailyEvent(Model):
    """Range partitioned by a date, open below, without a default partition."""

    day = fields.DateField()
    number = fields.IntField()
    name = fields.CharField(max_length=20, default="")
    pk = fields.CompositePrimaryKey("day", "number")

    class Meta:
        table_options = [
            PostgresqlTableOptions(
                partitioning=RangePartitioning(
                    fields=("day",),
                    partitions=[
                        RangePartition(
                            "before_2026", from_values=(RangeBound.MINVALUE,), to_values=(datetime.date(2026, 1, 1),)
                        ),
                        RangePartition(
                            "y2026", from_values=(datetime.date(2026, 1, 1),), to_values=(datetime.date(2027, 1, 1),)
                        ),
                    ],
                )
            )
        ]


class ReaderVisit(Model):
    """Without a primary key, hash partitioned by a foreign key - its key column."""

    reader = fields.ForeignKeyField("models.Reader", related_name="visits")
    page = fields.CharField(max_length=30)

    class Meta:
        primary_key = None
        table_options = [PostgresqlTableOptions(partitioning=HashPartitioning(fields=("reader",), partition_count=2))]
