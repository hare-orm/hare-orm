from hare import Model, fields


class CaseTableA(Model):
    id = fields.IntField(primary_key=True)

    class Meta:
        table = "Case_w1"


class CaseTableB(Model):
    id = fields.IntField(primary_key=True)
    x = fields.IntField()

    class Meta:
        table = "case_w1"
