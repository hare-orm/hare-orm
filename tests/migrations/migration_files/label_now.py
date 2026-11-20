from hare.fields.db_defaults.now import Now


class LabelNow(Now):
    """An importable subclass with constructor arguments and independent SQL."""

    def __init__(self, label):
        super().__init__()
        self.label = label

    def get_sql(self, dialect):
        return dialect.literals.get_string_literal_sql(self.label)

    def deconstruct(self):
        return f"{type(self).__module__}.{type(self).__qualname__}", [self.label], {}
