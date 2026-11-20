from hare.fields.db_defaults.uuid_v7 import UuidV7


class FixedUuidDefault(UuidV7):
    """An importable default whose overridden SQL needs no UUID generation feature."""

    required_feature = None

    def get_sql(self, dialect):
        return dialect.literals.get_string_literal_sql("fixed uuid")
