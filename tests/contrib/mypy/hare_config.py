"""The configuration the mypy plugin tests bind the typing models with."""

HARE_CONFIG = {
    "connections": {"typing": "sqlite+aiosqlite://:memory:"},
    "apps": {"typing": {"models": ["tests.contrib.mypy.models"], "default_connection": "typing"}},
    "swappable": {"READER_MODEL": "typing.Writer"},
}
