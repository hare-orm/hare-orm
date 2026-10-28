from hare import Model, fields


class JsonbNumberRow(Model):
    id = fields.IntField(primary_key=True)
    amount = fields.FloatField()
    numbers = fields.ArrayField(fields.FloatField())
    label = fields.TextField()
