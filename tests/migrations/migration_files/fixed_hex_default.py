from hare.fields.db_defaults.random_hex import RandomHex


class FixedHexDefault(RandomHex):
    """An argument-free subclass using the inherited deconstruct contract."""

    def get_sql(self, dialect):
        return dialect.literals.get_string_literal_sql("fixed hex")
