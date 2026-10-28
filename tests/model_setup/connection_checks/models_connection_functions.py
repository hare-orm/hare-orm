from hare import Model, fields
from hare.ddl.indexes import Index
from hare.fields.generated_field import GeneratedField
from hare.query.functions import Greatest


class GeneratedFloor(Model):
    id = fields.IntField(primary_key=True)
    qty = fields.IntField()
    floor = GeneratedField(expression=Greatest("qty", 3), output_field=fields.IntField())


class IndexedFloor(Model):
    id = fields.IntField(primary_key=True)
    qty = fields.IntField()

    class Meta:
        indexes = [Index(Greatest("qty", 3), name="indexed_floor_greatest")]
