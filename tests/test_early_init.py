import subprocess
import sys

import pytest

from hare import Hare, fields
from hare.contrib.pydantic import pydantic_model_creator
from hare.core.config import HareConfig
from hare.models import Model


class Tournament(Model):
    id = fields.IntField(primary_key=True)
    name = fields.CharField(max_length=100)
    created_at = fields.DatetimeField(auto_now_add=True)

    events: fields.ReverseRelation[Event]

    class Meta:
        ordering = ["name"]


class Event(Model):
    """
    The Event model docstring.

    This is multiline docs.
    """

    id = fields.IntField(primary_key=True)
    #: The Event NAME
    #:  It's pretty important
    name = fields.CharField(max_length=255)
    created_at = fields.DatetimeField(auto_now_add=True)
    tournament: fields.ForeignKeyNullableRelation[Tournament] = fields.ForeignKeyField(
        "models.Tournament", related_name="events", null=True
    )

    class Meta:
        ordering = ["name"]


@pytest.mark.asyncio
async def test_early_init():
    Event_TooEarly = pydantic_model_creator(Event)
    assert Event_TooEarly.model_json_schema() == {
        "title": "Event",
        "type": "object",
        "description": "The Event model docstring.<br/><br/>This is multiline docs.",
        "properties": {
            "id": {
                "title": "Id",
                "type": "integer",
                "maximum": 2147483647,
                "minimum": -2147483648,
            },
            "name": {
                "title": "Name",
                "type": "string",
                "description": "The Event NAME<br/>It's pretty important",
                "maxLength": 255,
            },
            "created_at": {
                "title": "Created At",
                "type": "string",
                "format": "date-time",
                "readOnly": True,
                "default": None,
            },
        },
        "required": ["id", "name"],
        "additionalProperties": False,
    }
    # Not bound yet: the relation is declared, its target not resolved.
    assert Event._meta.app is None
    assert Event._meta.fk_fields == {"tournament"}
    assert Event._meta.fields_map["tournament"].deconstruct()[2] == {
        "model_name": "models.Tournament",
        "related_name": "events",
        "null": True,
        "db_index": True,
    }

    Hare.bind_models(HareConfig.from_db_url("sqlite://:memory:", {"models": ["tests.test_early_init"]}))

    Event_Pydantic = pydantic_model_creator(Event)
    schema = Event_Pydantic.model_json_schema()
    # Extract the dynamic hash from $defs key
    tournament_def_key = [k for k in schema.get("$defs", {}) if k.startswith("Tournament_")][0]
    assert schema == {
        "$defs": {
            tournament_def_key: {
                "additionalProperties": False,
                "properties": {
                    "id": {
                        "maximum": 2147483647,
                        "minimum": -2147483648,
                        "title": "Id",
                        "type": "integer",
                    },
                    "name": {"maxLength": 100, "title": "Name", "type": "string"},
                    "created_at": {
                        "default": None,
                        "format": "date-time",
                        "readOnly": True,
                        "title": "Created At",
                        "type": "string",
                    },
                },
                "required": ["id", "name"],
                "title": "Tournament",
                "type": "object",
            }
        },
        "additionalProperties": False,
        "description": "The Event model docstring.<br/><br/>This is multiline docs.",
        "properties": {
            "id": {
                "maximum": 2147483647,
                "minimum": -2147483648,
                "title": "Id",
                "type": "integer",
            },
            "name": {
                "description": "The Event NAME<br/>It's pretty important",
                "maxLength": 255,
                "title": "Name",
                "type": "string",
            },
            "created_at": {
                "default": None,
                "format": "date-time",
                "readOnly": True,
                "title": "Created At",
                "type": "string",
            },
            "tournament": {
                "anyOf": [
                    {"$ref": f"#/$defs/{tournament_def_key}"},
                    {"type": "null"},
                ],
                "default": None,
                "nullable": True,
                "title": "Tournament",
            },
        },
        "required": ["id", "name"],
        "title": "Event",
        "type": "object",
    }
    # Bound: the relation resolved to its model and key column.
    assert Event._meta.full_name == "models.Event"
    assert Event._meta.fk_fields == {"tournament"}
    assert Event._meta.fields_map["tournament"].related_model is Tournament
    assert Event._meta.fields_map["tournament"].source_field == "tournament_id"
    assert Tournament._meta.backward_fk_fields == {"events"}


def test_init_of_models_without_triggers_does_not_load_the_migrations_package():
    """Bug: Hare.init() imported hare.migrations (operations, runner, autodetector, writer) for
    the name-collision check of Meta.triggers, even when no model declared a trigger - a model
    that declares one has imported that module itself already."""
    script = (
        "import asyncio, sys\n"
        "from hare import Hare, fields\n"
        "from hare.models import Model\n"
        "class Plain(Model):\n"
        "    id = fields.IntField(primary_key=True)\n"
        "    class Meta:\n"
        "        app = 'models'\n"
        "async def main():\n"
        "    await Hare.init(config={'connections': {'default': 'sqlite://:memory:'},\n"
        "        'apps': {'models': {'models': ['__main__'], 'default_connection': 'default'}}})\n"
        "    await Hare.close_connections()\n"
        "asyncio.run(main())\n"
        "print('hare.migrations' in sys.modules)\n"
    )
    completed = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=120
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip().splitlines()[-1] == "False"


def test_importing_validators_compiles_no_domain_pattern():
    """Bug: the module-level validate_domain_name compiled the large IDN domain pattern in its
    constructor, on every import of hare.fields - it compiles on first use now."""
    script = (
        "from hare.fields.validators import validate_domain_name\n"
        "print('_accept_idna_regex' in vars(validate_domain_name))\n"
        "validate_domain_name('example.com')\n"
        "print('_accept_idna_regex' in vars(validate_domain_name))\n"
    )
    completed = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=120
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.split() == ["False", "True"]


def test_importing_hare_loads_no_optional_dependency():
    """import hare imported pydantic, cryptography, anyio, importlib.metadata and tzlocal - each is
    imported where it is first used now."""
    script = (
        "import sys\n"
        "import hare\n"
        "names = ('pydantic', 'cryptography', 'anyio', 'importlib.metadata', 'tzlocal')\n"
        "print(sorted(name for name in names if name in sys.modules))\n"
    )
    completed = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=120
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip().splitlines()[-1] == "[]"


def test_first_filter_query_loads_no_set_operation_module():
    """The first filter of a process imported the set-operation query modules to check whether its
    plain value was a query."""
    script = (
        "import asyncio, sys\n"
        "from hare import Hare, fields\n"
        "from hare.models import Model\n"
        "class Plain(Model):\n"
        "    id = fields.IntField(primary_key=True)\n"
        "    name = fields.CharField(max_length=20)\n"
        "    class Meta:\n"
        "        app = 'models'\n"
        "async def main():\n"
        "    await Hare.init(config={'connections': {'default': 'sqlite://:memory:'},\n"
        "        'apps': {'models': {'models': ['__main__'], 'default_connection': 'default'}}})\n"
        "    await Hare.generate_schemas()\n"
        "    await Plain.objects.filter(name='a').first()\n"
        "    await Hare.close_connections()\n"
        "asyncio.run(main())\n"
        "print('hare.query.statements.select.combined_query' in sys.modules)\n"
    )
    completed = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=120
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip().splitlines()[-1] == "False"


def test_init_loads_only_the_driver_its_connection_uses():
    """Hare.init() on sqlite:// imported every built-in driver module and read the entry points
    of every installed package; it imports the SQLite driver only now, and registers the SQLite
    dialect alone - nothing of PostgreSQL is imported."""
    script = (
        "import asyncio, sys\n"
        "import importlib.metadata\n"
        "scans = []\n"
        "original_entry_points = importlib.metadata.entry_points\n"
        "def counting_entry_points(*args, **kwargs):\n"
        "    scans.append(kwargs)\n"
        "    return original_entry_points(*args, **kwargs)\n"
        "importlib.metadata.entry_points = counting_entry_points\n"
        "from hare import Hare, fields\n"
        "from hare.dialects.registry import DialectRegistry\n"
        "import hare.dialects.registry as registry_module\n"
        "registry_module.entry_points = counting_entry_points\n"
        "from hare.models import Model\n"
        "class Plain(Model):\n"
        "    id = fields.IntField(primary_key=True)\n"
        "    class Meta:\n"
        "        app = 'models'\n"
        "async def main():\n"
        "    await Hare.init(config={'connections': {'default': 'sqlite://:memory:'},\n"
        "        'apps': {'models': {'models': ['__main__'], 'default_connection': 'default'}}})\n"
        "    await Hare.generate_schemas()\n"
        "    await Plain.objects.create()\n"
        "    await Hare.close_connections()\n"
        "asyncio.run(main())\n"
        "print(sorted(name for name in sys.modules if name.startswith('hare.dialects.postgresql')))\n"
        "print(len(scans), sorted(str(name) for name in DialectRegistry.dialects_by_name))\n"
    )
    completed = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=120
    )
    assert completed.returncode == 0, completed.stderr
    drivers_line, registry_line = completed.stdout.strip().splitlines()[-2:]
    assert drivers_line == "[]"
    assert registry_line == "0 ['sqlite']"


def test_init_on_postgresql_loads_nothing_of_sqlite():
    """Hare.init() on postgresql:// registered every one of hare's own dialects and imported the
    SQLite dialect with them; it registers the PostgreSQL dialect alone now. The rust_pg driver
    connects on the first query, so no server is needed."""
    script = (
        "import asyncio, sys\n"
        "from hare import Hare, fields\n"
        "from hare.dialects.registry import DialectRegistry\n"
        "from hare.models import Model\n"
        "class Plain(Model):\n"
        "    id = fields.IntField(primary_key=True)\n"
        "    class Meta:\n"
        "        app = 'models'\n"
        "async def main():\n"
        "    await Hare.init(config={'connections': {'default': 'postgresql://user:secret@127.0.0.1:1/unused'},\n"
        "        'apps': {'models': {'models': ['__main__'], 'default_connection': 'default'}}})\n"
        "asyncio.run(main())\n"
        "prefixes = ('hare.dialects.sqlite', 'sqlite3', 'getpass')\n"
        "print(sorted(name for name in sys.modules if name.startswith(prefixes)))\n"
        "print(sorted(str(name) for name in DialectRegistry.dialects_by_name))\n"
    )
    completed = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=120
    )
    assert completed.returncode == 0, completed.stderr
    modules_line, registry_line = completed.stdout.strip().splitlines()[-2:]
    assert modules_line == "[]"
    assert registry_line == "['postgresql']"


def test_dialects_are_listed_in_builtin_order_whichever_registered_first():
    script = (
        "from hare.dialects.registry import DialectRegistry\n"
        "DialectRegistry.get_dialect('postgresql')\n"
        "print([str(dialect.name) for dialect in DialectRegistry.get_dialects()][:3])\n"
    )
    completed = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=120
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "['sql', 'sqlite', 'postgresql']"


def test_loading_the_builtin_dialects_imports_no_sqlite3_and_no_zone_data():
    """Hare.init() on a PostgreSQL URL imported sqlite3 and opened an in-memory SQLite connection
    (the SQLite dialect's constants probed the bind-parameter limit), and loaded the UTC zone
    through tzdata to validate the default timezone - a few milliseconds of every start."""
    script = (
        "import sys\n"
        "from hare.dialects.registry import DialectRegistry\n"
        "from hare.utils.timezone import Timezone\n"
        "DialectRegistry.load_builtin_dialects()\n"
        "Timezone.validate('UTC')\n"
        "print('sqlite3' in sys.modules, 'importlib.resources' in sys.modules)\n"
    )
    completed = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=120
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.split() == ["False", "False"]


def test_sqlite_client_reads_the_bind_parameter_limit():
    from hare.dialects.sqlite.client import SqliteClient

    assert SqliteClient.features.max_bind_parameters == SqliteClient.read_max_bind_parameters()
    assert SqliteClient.features.max_bind_parameters >= 999


def test_sqlite_client_reads_the_cascade_depth_limit():
    """The depth a native ON DELETE CASCADE stops at differs between SQLite builds (100 in the
    official 3.37.2 library, 1000 in newer ones) - it is read off the build, never assumed."""
    import sqlite3
    from contextlib import closing

    from hare.dialects.sqlite.client import SqliteClient

    with closing(sqlite3.connect(":memory:")) as connection:
        build_limit = connection.getlimit(sqlite3.SQLITE_LIMIT_TRIGGER_DEPTH)
    assert SqliteClient.features.cascade_depth_limit == build_limit
