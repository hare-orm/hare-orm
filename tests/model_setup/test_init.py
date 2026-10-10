import os
import re
from unittest.mock import patch

import pytest

from hare import Connections, Hare
from hare.core.config import AppConfig, ConnectionConfig, HareConfig
from hare.core.hare_context import HareContext
from hare.exceptions import ConfigurationError

# Save the original classproperty before any test can shadow it
_original_apps_prop = Hare.__dict__["apps"]


async def _reset_hare():
    """Helper to reset Hare state before each test.

    Note: We MUST NOT set Hare.apps = None
    because it is a classproperty and setting it shadows the property
    with a class attribute, breaking future access.
    """
    # Restore the original classproperty if it was shadowed
    if not isinstance(Hare.__dict__.get("apps"), type(_original_apps_prop)):
        type.__setattr__(Hare, "apps", _original_apps_prop)

    # Get the current context and properly reset it
    ctx = HareContext.get_current()
    if ctx is not None:
        # Clear db_config first to prevent close_all from trying to import bad backends
        if ctx._connections is not None:
            # Clear storage without closing (to avoid importing bad backends)
            ctx._connections._storage.clear()
            ctx._connections._db_config = None
            ctx._connections = None
        ctx._apps = None
        ctx._inited = False
        ctx._default_connection = None
    else:
        # No context exists - create one for the test
        ctx = HareContext()
        ctx.__enter__()


@pytest.mark.asyncio
async def test_basic_init():
    await _reset_hare()
    await Hare.init(
        {
            "connections": {
                "default": {
                    "engine": "sqlite+aiosqlite",
                    "credentials": {"file_path": ":memory:"},
                }
            },
            "apps": {"models": {"models": ["tests.testmodels"], "default_connection": "default"}},
        }
    )
    assert "models" in Hare.apps
    assert Connections.get("default") is not None


@pytest.mark.asyncio
async def test_dataclass_init():
    await _reset_hare()
    await Hare.init(
        config=HareConfig(
            connections={
                "default": ConnectionConfig(
                    engine="sqlite+aiosqlite",
                    credentials={"file_path": ":memory:"},
                )
            },
            apps={
                "models": AppConfig(
                    models=["tests.testmodels"],
                    default_connection="default",
                )
            },
        )
    )
    assert "models" in Hare.apps
    assert Connections.get("default") is not None


@pytest.mark.asyncio
async def test_empty_modules_init():
    await _reset_hare()
    with pytest.warns(RuntimeWarning, match='Module "tests.model_setup" has no models'):
        await Hare.init(
            {
                "connections": {
                    "default": {
                        "engine": "sqlite+aiosqlite",
                        "credentials": {"file_path": ":memory:"},
                    }
                },
                "apps": {"models": {"models": ["tests.model_setup"], "default_connection": "default"}},
            }
        )


@pytest.mark.parametrize(
    ("error_match", "models_module"),
    [
        pytest.param(
            'backward relation "events" duplicates in model Tournament',
            "tests.model_setup.models_dup1",
            id="dup1_init",
        ),
        pytest.param(
            'backward relation "events" duplicates in model Team', "tests.model_setup.models_dup2", id="dup2_init"
        ),
        pytest.param(
            'backward relation "event" duplicates in model Tournament', "tests.model_setup.models_dup3", id="dup3_init"
        ),
        pytest.param(
            "Field 'val' \\(CharField\\) can't be DB-generated",
            "tests.model_setup.model_generated_nonint",
            id="generated_nonint",
        ),
        pytest.param(
            "Can't create model Tournament with two primary keys, only single primary key is supported",
            "tests.model_setup.model_multiple_pk",
            id="multiple_pk",
        ),
        pytest.param(
            "Can't create model Tournament without explicit primary key if field 'id' already present",
            "tests.model_setup.model_nonpk_id",
            id="nonpk_id",
        ),
    ],
)
@pytest.mark.asyncio
async def test_bad_model_declaration_fails_init(error_match, models_module):
    await _reset_hare()
    with pytest.raises(ConfigurationError, match=error_match):
        await Hare.init(
            {
                "connections": {
                    "default": {
                        "engine": "sqlite+aiosqlite",
                        "credentials": {"file_path": ":memory:"},
                    }
                },
                "apps": {
                    "models": {
                        "models": [models_module],
                        "default_connection": "default",
                    }
                },
            }
        )


@pytest.mark.asyncio
async def test_unknown_connection():
    await _reset_hare()
    with pytest.raises(
        ConfigurationError,
        match='App "models" refers to unknown connection "fioop"',
    ):
        await Hare.init(
            {
                "connections": {
                    "default": {
                        "engine": "sqlite+aiosqlite",
                        "credentials": {"file_path": ":memory:"},
                    }
                },
                "apps": {"models": {"models": ["tests.testmodels"], "default_connection": "fioop"}},
            }
        )


@pytest.mark.asyncio
async def test_init_connections_false():
    await _reset_hare()
    config = {
        "connections": {
            "default": {
                "engine": "sqlite+aiosqlite",
                "credentials": {"file_path": ":memory:"},
            }
        },
        "apps": {"models": {"models": ["tests.testmodels"], "default_connection": "default"}},
    }
    with (
        patch("hare.core.connections.connection_handler.ConnectionHandler._init") as mocked_init,
        patch("hare.core.connections.connection_handler.ConnectionHandler.get") as mocked_get,
    ):
        await Hare.init(config=config, connect=False)
        mocked_init.assert_not_called()
        mocked_get.assert_not_called()
    assert "models" in Hare.apps
    assert Connections.current().db_config == config["connections"]


@pytest.mark.asyncio
async def test_init_connections_false_with_create_db():
    await _reset_hare()
    config = {
        "connections": {
            "default": {
                "engine": "sqlite+aiosqlite",
                "credentials": {"file_path": ":memory:"},
            }
        },
        "apps": {"models": {"models": ["tests.testmodels"], "default_connection": "default"}},
    }
    with pytest.raises(ConfigurationError, match="connect=False cannot be used with _create_db=True"):
        await Hare.init(config=config, _create_db=True, connect=False)


@pytest.mark.asyncio
async def test_init_connections_false_then_running_a_query_raises_clear_error():
    """Building a queryset needs no connection; running one on a model whose connection was
    never initialized (basetable/basequery are still MetaInfo's dialect-less placeholders) raises a
    clear ConfigurationError instead of crashing deep inside query construction. Manipulates
    Tournament's already-finalised _meta directly (rather than going through a second
    Hare.init(connect=False) call) because tests.testmodels is
    shared process-wide - an earlier test in this session has already given Tournament real
    basetable/basequery, which connect=False's own early-return would leave untouched."""
    from hare.sql import Query, Table
    from tests.testmodels import Tournament

    await _reset_hare()
    await Hare.init(
        {
            "connections": {
                "default": {
                    "engine": "sqlite+aiosqlite",
                    "credentials": {"file_path": ":memory:"},
                }
            },
            "apps": {"models": {"models": ["tests.testmodels"], "default_connection": "default"}},
        }
    )

    original_basetable = Tournament._meta.basetable
    original_basequery = Tournament._meta.basequery
    Tournament._meta.basetable = Table("")
    Tournament._meta.basequery = Query()
    try:
        queryset = Tournament.objects.all()
        with pytest.raises(ConfigurationError, match="Hare.init\\(\\) hasn't set up its connections yet"):
            await queryset
    finally:
        Tournament._meta.basetable = original_basetable
        Tournament._meta.basequery = original_basequery


@pytest.mark.asyncio
async def test_url_config_needs_a_modules_mapping():
    await _reset_hare()
    with pytest.raises(ConfigurationError, match="modules must be a mapping of app label to module paths"):
        await Hare.init(HareConfig.from_db_url("sqlite+aiosqlite://:memory:", ["tests.testmodels"]))


@pytest.mark.asyncio
async def test_default_connection_init():
    await _reset_hare()
    await Hare.init(
        {
            "connections": {
                "default": {
                    "engine": "sqlite+aiosqlite",
                    "credentials": {"file_path": ":memory:"},
                }
            },
            "apps": {"models": {"models": ["tests.testmodels"]}},
        }
    )
    assert "models" in Hare.apps
    assert Connections.get("default") is not None


@pytest.mark.asyncio
async def test_db_url_init():
    await _reset_hare()
    await Hare.init(
        {
            "connections": {"default": f"sqlite+aiosqlite://{':memory:'}"},
            "apps": {"models": {"models": ["tests.testmodels"], "default_connection": "default"}},
        }
    )
    assert "models" in Hare.apps
    assert Connections.get("default") is not None


@pytest.mark.asyncio
async def test_shorthand_init():
    await _reset_hare()
    await Hare.init(HareConfig.from_db_url(f"sqlite+aiosqlite://{':memory:'}", {"models": ["tests.testmodels"]}))
    assert "models" in Hare.apps
    assert Connections.get("default") is not None


@pytest.mark.asyncio
async def test_init_wrong_connection_engine():
    await _reset_hare()
    with pytest.raises(ConfigurationError, match='Unknown database engine "hare.dialects.test"'):
        await Hare.init(
            {
                "connections": {
                    "default": {
                        "engine": "hare.dialects.test",
                        "credentials": {"file_path": ":memory:"},
                    }
                },
                "apps": {"models": {"models": ["tests.testmodels"], "default_connection": "default"}},
            }
        )


@pytest.mark.asyncio
async def test_init_wrong_connection_engine_2():
    await _reset_hare()
    with pytest.raises(
        ConfigurationError,
        match='Unknown database engine "hare.dialects.sqlite": expected one of',
    ):
        await Hare.init(
            {
                "connections": {
                    "default": {
                        "engine": "hare.dialects.sqlite",
                        "credentials": {"file_path": ":memory:"},
                    }
                },
                "apps": {"models": {"models": ["tests.testmodels"], "default_connection": "default"}},
            }
        )


@pytest.mark.asyncio
async def test_init_no_connections():
    await _reset_hare()
    with pytest.raises(ConfigurationError, match='Config must define "connections" section'):
        await Hare.init({"apps": {"models": {"models": ["tests.testmodels"], "default_connection": "default"}}})


@pytest.mark.asyncio
async def test_init_no_apps():
    await _reset_hare()
    with pytest.raises(ConfigurationError, match='Config must define "apps" section'):
        await Hare.init(
            {
                "connections": {
                    "default": {
                        "engine": "sqlite+aiosqlite",
                        "credentials": {"file_path": ":memory:"},
                    }
                }
            }
        )


@pytest.mark.asyncio
async def test_init_takes_one_configuration_source():
    await _reset_hare()
    with pytest.raises(TypeError, match="config_file"):
        await Hare.init(
            {
                "connections": {
                    "default": {
                        "engine": "sqlite+aiosqlite",
                        "credentials": {"file_path": ":memory:"},
                    }
                },
                "apps": {"models": {"models": ["tests.testmodels"], "default_connection": "default"}},
            },
            config_file="file.json",
        )
    with pytest.raises(
        ConfigurationError, match="config must be a HareConfig, a dict, a file path or 'module.VARIABLE'"
    ):
        await Hare.init(42)


@pytest.mark.asyncio
async def test_init_config_file_wrong_extension():
    await _reset_hare()
    with pytest.raises(
        ConfigurationError,
        match=r"Unknown config extension .ini, only .yml, .yaml, .json are supported",
    ):
        await Hare.init("settings/config.ini")
    # Without a path separator the text names ``module.VARIABLE``.
    with pytest.raises(ConfigurationError, match="Cannot import configuration module 'no_such_settings'"):
        await Hare.init("no_such_settings.ini")


@pytest.mark.skipif(os.name == "nt", reason="path issue on Windows")
@pytest.mark.asyncio
async def test_init_json_file():
    await _reset_hare()
    await Hare.init(os.path.dirname(__file__) + "/init.json")
    assert "models" in Hare.apps
    assert Connections.get("default") is not None


@pytest.mark.skipif(os.name == "nt", reason="path issue on Windows")
@pytest.mark.asyncio
async def test_init_yaml_file():
    await _reset_hare()
    await Hare.init(os.path.dirname(__file__) + "/init.yaml")
    assert "models" in Hare.apps
    assert Connections.get("default") is not None


@pytest.mark.asyncio
async def test_generate_schema_without_init():
    await _reset_hare()
    with pytest.raises(ConfigurationError, match=r"Call init\(\) before generating schemas"):
        await Hare.generate_schemas()


@pytest.mark.asyncio
async def test_drop_databases_without_init():
    await _reset_hare()
    with pytest.raises(ConfigurationError, match=r"Call init\(\) before dropping databases"):
        await Hare._drop_databases()


@pytest.mark.asyncio
async def test_bad_models():
    await _reset_hare()
    with pytest.raises(ConfigurationError, match='Module "tests.testmodels2" not found'):
        await Hare.init(
            {
                "connections": {
                    "default": {
                        "engine": "sqlite+aiosqlite",
                        "credentials": {"file_path": ":memory:"},
                    }
                },
                "apps": {"models": {"models": ["tests.testmodels2"], "default_connection": "default"}},
            }
        )


@pytest.mark.asyncio
async def test_generated_int_field_outside_the_primary_key_is_rejected_by_schema_generation():
    """IntField's GENERATED_SQL is an auto-increment primary key definition - a generated non-pk
    IntField (a mapped IDENTITY column) used to render a second PRIMARY KEY into the table DDL."""
    import sys
    import types

    from hare import fields
    from hare.models import Model

    class Counter(Model):
        sequence = fields.IntField(generated=True)

    await _reset_hare()
    module = types.ModuleType("tests.model_setup.generated_int_outside_primary_key")
    Counter.__module__ = module.__name__
    module.Counter = Counter
    sys.modules[module.__name__] = module
    try:
        await Hare.init(HareConfig.from_db_url("sqlite+aiosqlite://:memory:", {"models": [module.__name__]}))
        with pytest.raises(ConfigurationError, match=r"Field 'sequence' \(IntField\) is DB-generated outside"):
            await Hare.generate_schemas()
    finally:
        del sys.modules[module.__name__]
        await Hare.close_connections()


def test_generated_field_without_generation_support_outside_the_primary_key_is_rejected():
    from hare import fields
    from hare.models import Model

    with pytest.raises(ConfigurationError, match=r"Field 'code' \(CharField\) on model Coded can't be DB-generated"):

        class Coded(Model):
            code = fields.CharField(max_length=5, generated=True)


def _build_duplicate_column_models(case: str) -> list[type]:
    from hare import fields
    from hare.models import Model

    if case == "two_fields_one_source_field":

        class TwinColumns(Model):
            alpha = fields.IntField(source_field="shared")
            beta = fields.IntField(source_field="shared")

        return [TwinColumns]
    if case == "source_field_is_another_field_name":

        class ShadowedColumn(Model):
            alpha = fields.IntField(source_field="beta")
            beta = fields.IntField()

        return [ShadowedColumn]
    if case == "foreign_key_source_field_is_a_plain_field":

        class ColumnTarget(Model):
            pass

        class ColumnOwner(Model):
            target = fields.ForeignKeyField("models.ColumnTarget", source_field="value")
            value = fields.IntField()

        return [ColumnTarget, ColumnOwner]

    class PrimaryKeyColumnOwner(Model):
        id = fields.IntField(primary_key=True, source_field="name")
        name = fields.CharField(max_length=5)

    return [PrimaryKeyColumnOwner]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("case", "expected_message"),
    [
        (
            "two_fields_one_source_field",
            "Fields 'alpha' and 'beta' on TwinColumns both map to the database column 'shared'",
        ),
        (
            "source_field_is_another_field_name",
            "Fields 'alpha' and 'beta' on ShadowedColumn both map to the database column 'beta'",
        ),
        (
            "foreign_key_source_field_is_a_plain_field",
            "Fields 'value' and 'target' on ColumnOwner both map to the database column 'value'",
        ),
        (
            "primary_key_source_field_is_a_plain_field",
            "Fields 'id' and 'name' on PrimaryKeyColumnOwner both map to the database column 'name'",
        ),
    ],
)
async def test_two_fields_mapped_to_one_column_are_rejected(case, expected_message):
    """Two fields sharing one database column used to pass declaration and only fail later with a
    "duplicate column" error from the database, or silently read/write the wrong column."""
    import sys
    import types

    await _reset_hare()
    module = types.ModuleType(f"tests.model_setup.duplicate_columns_{case}")
    for model in _build_duplicate_column_models(case):
        model.__module__ = module.__name__
        setattr(module, model.__name__, model)
    sys.modules[module.__name__] = module
    try:
        with pytest.raises(ConfigurationError, match=re.escape(expected_message)):
            await Hare.init(HareConfig.from_db_url("sqlite+aiosqlite://:memory:", {"models": [module.__name__]}))
    finally:
        del sys.modules[module.__name__]
