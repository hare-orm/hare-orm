"""A Meta.managed = False model referenced by a managed one."""

from hare import fields
from hare.models import Model


class ExternalReport(Model):
    id = fields.IntField(primary_key=True)
    title = fields.CharField(max_length=50)

    class Meta:
        table = "external_report"
        managed = False


class ReportNote(Model):
    id = fields.IntField(primary_key=True)
    report: fields.ForeignKeyRelation[ExternalReport] = fields.ForeignKeyField(
        "models.ExternalReport", related_name="notes"
    )

    class Meta:
        table = "report_note"
