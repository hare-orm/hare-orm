"""What the health route tests of every framework share: the configurations and a criterion that
always degrades."""

import os

from hare.health.criteria import HealthCriterion

HEALTHY_CONFIG = {
    "connections": {"default": "sqlite+aiosqlite://:memory:"},
    "apps": {"models": {"models": ["tests.contrib.frameworks.models"], "default_connection": "default"}},
}


def make_unhealthy_config(directory: str) -> dict:
    """A configuration with a connection to a file SQLite can't open."""
    return {
        "connections": {
            "default": "sqlite+aiosqlite://:memory:",
            "unreachable": f"sqlite+aiosqlite://{os.path.join(directory, 'no_such_directory', 'db.sqlite3')}",
        },
        "apps": {"models": {"models": ["tests.contrib.frameworks.models"], "default_connection": "default"}},
    }


class AlwaysDegraded(HealthCriterion):
    """A criterion of one's own that always holds."""

    def check(self, connection_check):
        return f"{connection_check.connection_alias} is degraded on purpose"
