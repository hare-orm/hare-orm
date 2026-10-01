import re

#: Connection alias used when none is explicitly configured.
DEFAULT_CONNECTION_NAME = "default"

#: Environment variable pointing at a Hare ORM config object (module.VARIABLE).
ENV_HARE_ORM_CONFIG = "HARE_ORM"

#: pytest-xdist sets this to the worker's id (e.g. "gw0") in each worker process, and leaves it
#: unset in a plain (non-`-n`) pytest run. Used by DbUrlConfigGenerator.expand() to keep a fixed,
#: non-templated sqlite file `db_url` from colliding between workers under `-n`.
ENV_PYTEST_XDIST_WORKER = "PYTEST_XDIST_WORKER"

#: Placeholder a password or other secret connection-config value is replaced with in logs and reprs.
PASSWORD_LOG_MASK = "***"  # nosec B105 - a masking placeholder, not a real credential

#: Lower-cased fragments marking a connection-config key (credential, DB_URL query parameter) as
#: secret - its value is masked in startup logs and in connection-config reprs.
SECRET_CONFIG_KEY_MARKERS = ("password", "passwd", "pwd", "secret", "token")

#: A ``swappable`` config setting name - an upper-case identifier such as ``USER_MODEL``.
SWAPPABLE_SETTING_NAME_PATTERN = re.compile(r"[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)*")

#: The model label a ``swappable`` config setting points at - ``app_label.ModelName``.
SWAPPABLE_MODEL_LABEL_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\.[A-Za-z_][A-Za-z0-9_]*")

#: File extensions from_config_file() accepts, and which loader each maps to.
YAML_CONFIG_FILE_EXTENSIONS = (".yml", ".yaml")

JSON_CONFIG_FILE_EXTENSIONS = (".json",)

SUPPORTED_CONFIG_FILE_EXTENSIONS = YAML_CONFIG_FILE_EXTENSIONS + JSON_CONFIG_FILE_EXTENSIONS

#: What ``Cache.get()`` hands back for a key it holds no value for.
CACHE_MISS = object()

#: Environment variable setting how many entries a bucket of a statement plan cache (``Cache``) keeps.
ENV_STATEMENT_PLAN_CACHE_MAX_SIZE_PER_MODEL = "HARE_STATEMENT_PLAN_CACHE_MAX_SIZE_PER_MODEL"

#: The default: enough for one model's variety of queries, while bounding the growth from `__in=`
#: lists of ever different lengths.
DEFAULT_STATEMENT_PLAN_CACHE_MAX_SIZE_PER_MODEL = 512
