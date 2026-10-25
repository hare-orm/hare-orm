"""The configuration the mypy plugin tests bind the PostgreSQL typing models with - a run of its own: the
QuerySet methods a dialect registers are those of every connection's dialect."""

HARE_CONFIG = {
    # Never connected to - the plugin reads the dialect of its scheme.
    "connections": {"typing_postgresql": "postgresql://postgres:postgres@127.0.0.1:5433/typing"},
    "apps": {
        "typing_postgresql": {
            "models": ["tests.contrib.mypy.postgresql_models"],
            "default_connection": "typing_postgresql",
        }
    },
}
