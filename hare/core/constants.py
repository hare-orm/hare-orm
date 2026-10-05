from __future__ import annotations

import re

#: Connection alias used when none is explicitly configured.
DEFAULT_CONNECTION_NAME = "default"

#: Environment variable pointing at a Hare ORM config object (module.VARIABLE).
ENV_HARE_ORM_CONFIG = "HARE_ORM"

#: pytest-xdist sets this to the worker's id (e.g. "gw0") in each worker process, and leaves it
#: unset in a plain (non-`-n`) pytest run. Used by DbUrlConfigGenerator.expand() to keep a fixed,
#: non-templated sqlite file `db_url` from colliding between workers under `-n`.
ENV_PYTEST_XDIST_WORKER = "PYTEST_XDIST_WORKER"


#: A ``swappable`` config setting name - an upper-case identifier such as ``USER_MODEL``.
SWAPPABLE_SETTING_NAME_PATTERN = re.compile(r"[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)*")


#: File extensions from_config_file() accepts, and which loader each maps to.
YAML_CONFIG_FILE_EXTENSIONS = (".yml", ".yaml")

JSON_CONFIG_FILE_EXTENSIONS = (".json",)

SUPPORTED_CONFIG_FILE_EXTENSIONS = YAML_CONFIG_FILE_EXTENSIONS + JSON_CONFIG_FILE_EXTENSIONS

#: What ``Cache.get()`` hands back for a key it holds no value for.
CACHE_MISS = object()


#: The rows from which a table counts as large for the migration safety check, by default - an
#: operation locking a table this big keeps its readers or writers waiting noticeably.
DEFAULT_LARGE_TABLE_ROWS = 100_000
#: The largest ``migrations.safety.large_table_rows`` setting taken.
MAX_LARGE_TABLE_ROWS = 10**12
