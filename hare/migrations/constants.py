import re

from hare.fields.relations.fields.foreign_key_field_instance import ForeignKeyFieldInstance
from hare.fields.relations.fields.many_to_many_field_instance import ManyToManyFieldInstance
from hare.fields.relations.fields.one_to_one_field_instance import OneToOneFieldInstance
from hare.models.enums import ModelOption

#: ``python -m ruff`` argument lists makemigrations formats new migration files with, in order -
#: import sorting first, then the formatter.
MIGRATION_FORMATTER_RUFF_ARGUMENTS: tuple[tuple[str, ...], ...] = (
    ("check", "--select", "I", "--fix", "--quiet"),
    ("format", "--quiet"),
)


#: A type cast Postgres appends to a default expression it re-serializes - ``'a'::text``,
#: ``(0)::numeric(10,2)``, ``'{}'::text[]``.
SQL_TYPE_CAST_RE = re.compile(r"::\s*[a-z_][a-z0-9_ ]*(?:\(\s*\d+(?:\s*,\s*\d+)?\s*\))?(?:\s*\[\s*\])*")
#: Whitespace and parentheses - left out of a default expression's fingerprint, since the
#: database adds and drops both when it echoes the expression back.
SQL_FINGERPRINT_NOISE_RE = re.compile(r"[\s()]+")
#: The fingerprint of a default taking the current time, whichever spelling it has.
CURRENT_TIME_DEFAULT_FINGERPRINT = "now"

#: The collation providers CreateCollation takes.
COLLATION_PROVIDERS = frozenset({"libc", "icu"})


RELATION_FIELDS = (ForeignKeyFieldInstance, OneToOneFieldInstance, ManyToManyFieldInstance)


MIGRATION_NUMBER_RE = re.compile(r"^(\d{4})_")


#: Sentinel migration name resolving to the newest migration in an app.
LATEST_MIGRATION = "__latest__"


#: Sentinel migration name a dependency gives for the app's first (root) migration.
FIRST_MIGRATION = "__first__"


#: The ``migrate`` target unapplying every migration of an app - ``migrate app zero``.
ZERO_MIGRATION = "zero"


DIRECT_RELATION_FIELDS = (
    ForeignKeyFieldInstance,
    ManyToManyFieldInstance,
    OneToOneFieldInstance,
)


MIGRATION_SLUG_RE = re.compile(r"[^\w]+")

#: The names every migration file binds itself - an enum the migration declares never takes one.
MIGRATION_MODULE_NAMES = frozenset({"migrations", "ops", "Migration"})


#: The module name a migration read from its source (``Migration.from_source()``) runs under -
#: followed by ``<app_label>.<migration_name>``.
RUNTIME_MIGRATION_MODULE_PREFIX = "hare_runtime_migration."

#: The share of a renamed model's fields that must still match by content for a rename that also
#: adds or removes a field. Conservative: a wrong pairing puts the old rows under the wrong schema.
MODEL_RENAME_FIELD_SIMILARITY_THRESHOLD = 0.5

#: Model options each changed by an operation of its own (AlterModelTable, AlterModelSchema,
#: AddIndex, AddConstraint, AddTrigger, ...) - every other option is changed by AlterModelOptions,
#: which carries the whole set of them.
MODEL_OPTIONS_WITH_OWN_OPERATIONS = frozenset(
    {
        ModelOption.TABLE,
        ModelOption.TABLE_IS_EXPLICIT,
        ModelOption.SCHEMA,
        ModelOption.APP,
        ModelOption.INDEXES,
        ModelOption.CONSTRAINTS,
        ModelOption.TRIGGERS,
        ModelOption.PK_ATTR,
    }
)

#: Appended to an automatic M2M through table's name while it's moved aside, so a through model's
#: table (or the other way around) can take over the same name and receive its rows.
M2M_THROUGH_TABLE_SWAP_SUFFIX = "__swap"

#: The longest line a migration file writes an operation on before splitting it one argument per
#: line - the formatter's own line length.
MIGRATION_LINE_LENGTH = 119
