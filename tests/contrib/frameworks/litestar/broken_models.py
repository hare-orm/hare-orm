from hare import fields
from hare.models import Model


class Draft(Model):
    id = fields.IntField(primary_key=True)
    title = fields.CharField(max_length=100)
