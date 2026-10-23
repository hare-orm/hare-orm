from __future__ import annotations

import uuid

#: Database URL of a fresh in-memory SQLite database.
MEMORY_SQLITE = "sqlite+aiosqlite://:memory:"

#: Environment variable switching hare_test_context()'s reusable Postgres test databases on
#: ("1"/"true") or off ("0"/"false") when its reuse_databases argument is left as None.
REUSE_DATABASES_ENVIRONMENT_VARIABLE = "HARE_TEST_REUSE_DATABASES"

#: Accepted values of REUSE_DATABASES_ENVIRONMENT_VARIABLE, compared case-insensitively.
REUSE_DATABASES_ENABLED_VALUES = frozenset({"1", "true"})
REUSE_DATABASES_DISABLED_VALUES = frozenset({"0", "false"})

#: Namespace of the uuid5 ids filling a reusable test database name's "{}" placeholder - the id
#: of a slot depends only on its number and the pytest-xdist worker, never on randomness.
REUSABLE_DATABASE_SLOT_NAMESPACE = uuid.UUID("5f0a8f64-3c1e-4b8a-9d7e-2b6c1f4e9a31")
