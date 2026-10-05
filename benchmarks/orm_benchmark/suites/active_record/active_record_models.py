"""The widget, gadget and tag models on the ORM of ``ActiveRecordOrm.current`` - module-level classes,
as hare and tortoise-orm find their models by scanning a module. Imported once per ``run`` process,
after ``ActiveRecordOrm.current`` is set."""

from __future__ import annotations

from orm_benchmark.measuring.workload import Workload
from orm_benchmark.suites.active_record.active_record_orm import ActiveRecordOrm

orm = ActiveRecordOrm.current
assert orm is not None, "set ActiveRecordOrm.current before importing the models"
fields = orm.fields


class Widget(orm.model):  # type: ignore[name-defined]
    id = fields.IntField(primary_key=True)
    name = fields.CharField(max_length=100)
    category = fields.CharField(max_length=20)
    value = fields.IntField(default=0)
    score = fields.FloatField(default=0.0)
    payload = fields.JSONField(default=Workload.get_default_payload)
    tags = fields.ManyToManyField("models.Tag", related_name="widgets")

    class Meta:
        app = "models"


class Gadget(orm.model):  # type: ignore[name-defined]
    id = fields.IntField(primary_key=True)
    widget = fields.ForeignKeyField("models.Widget", related_name="gadgets")
    label = fields.CharField(max_length=50)

    class Meta:
        app = "models"


class Tag(orm.model):  # type: ignore[name-defined]
    id = fields.IntField(primary_key=True)
    name = fields.CharField(max_length=50)

    class Meta:
        app = "models"
