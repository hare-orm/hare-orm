from hare import fields
from hare.models import Model


class RollbackRecord(Model):
    id = fields.IntField(primary_key=True, generated=False)
    number = fields.IntField()

    class Meta:
        track_dirty_fields = True


class CompositeRollbackRecord(Model):
    batch = fields.IntField()
    sequence = fields.IntField()
    pk = fields.CompositePrimaryKey("batch", "sequence")
    number = fields.IntField()

    class Meta:
        track_dirty_fields = True
