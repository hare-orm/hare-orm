from hare import Model, fields


class Declarations:
    class ScalarBase(Model):
        code = fields.IntField()

        class Meta:
            abstract = True

    class Nested:
        class CompositeBase(Model):
            number = fields.IntField()
            region = fields.IntField()
            code = fields.IntField()
            pk = fields.CompositePrimaryKey("region", "number")

            class Meta:
                abstract = True


class ScalarEntry(Declarations.ScalarBase):
    pass


class CompositeEntry(Declarations.Nested.CompositeBase):
    pass
