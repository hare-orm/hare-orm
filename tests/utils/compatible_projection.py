class CompatibleProjection:
    """A valid projection protocol implementation with no builtin projection internals."""

    name = "by_score"

    def for_binding(self, binding):
        return self

    def with_sql_query(self, binding, client=None):
        return self

    def get_definition_sql(self, binding, editor):
        return f"{editor.quote(self.name)} (SELECT score ORDER BY score)"

    def get_read_columns(self):
        return ("score",)
