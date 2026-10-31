from hare import Model, fields


class CaseColumns(Model):
    id = fields.IntField(primary_key=True)
    upper_name = fields.CharField(max_length=10, source_field="Name")
    lower_name = fields.CharField(max_length=10, source_field="name")
