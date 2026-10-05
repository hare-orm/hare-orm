"""Hare.init()/HareContext.init() configuration handling: every accepted config shape works,
conflicting or malformed arguments raise ConfigurationError, and a failed init leaves the
process-wide state exactly as it was."""

from __future__ import annotations

import os
import sys
import types
import uuid

import pytest

from hare import Hare, fields
from hare.contrib.test import requires_features
from hare.contrib.test.isolated_contexts import hare_test_context
from hare.core.config import AppConfig, ConfigSecrets, ConnectionConfig, DBUrlConfig, HareConfig
from hare.core.hare_context import HareContext
from hare.dialects.base.connection.db_url_config_generator import DbUrlConfigGenerator
from hare.dialects.base.constants import SLOW_QUERY_THRESHOLD_MS
from hare.dialects.postgresql.drivers.asyncpg.client import AsyncpgClient
from hare.exceptions import ConfigurationError
from hare.instrumentation.observers.observers import Observers
from hare.models import Model

MEMORY_CONFIG = {
    "connections": {"default": "sqlite+aiosqlite://:memory:"},
    "apps": {"models": {"models": ["tests.testmodels"]}},
}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "config",
    [
        {
            "connections": {"default": {"db_url": "sqlite+aiosqlite://:memory:"}},
            "apps": {"models": {"models": ["tests.testmodels"]}},
        },
        HareConfig(
            connections={"default": ConnectionConfig(db_url="sqlite+aiosqlite://:memory:")},
            apps={"models": AppConfig(models=["tests.testmodels"])},
        ),
    ],
)
async def test_connection_given_as_a_db_url_mapping_works(config):
    """ConnectionConfig(db_url=...) and its {"db_url": ...} dict form used to fail with
    ValueError: Empty module name."""
    async with HareContext() as ctx:
        await ctx.init(config=config)
        await ctx.generate_schemas()
        assert (await ctx.get_connection().execute_dicts("SELECT 1 AS one")) == [{"one": 1}]


def test_postgres_driver_choice_survives_building_a_second_client():
    """A second client for the same alias (create_independent(), a loop switch) gets the same
    driver as the first."""
    config = {
        "connections": {
            "default": {
                "engine": "postgresql+asyncpg",
                "credentials": {"host": "127.0.0.1", "user": "postgres", "database": "db"},
            }
        },
        "apps": {"models": {"models": ["tests.testmodels"]}},
    }
    handler_context = HareContext()
    handler_context.connections._init_config(HareConfig.from_dict(config).to_dict()["connections"])

    first_client = handler_context.connections.create_independent("default")
    second_client = handler_context.connections.create_independent("default")

    assert type(first_client) is AsyncpgClient
    assert type(second_client) is AsyncpgClient


@pytest.mark.parametrize(
    ("connections_config", "secret"),
    [
        ({"default": "postgresql+asyncpg://127.0.0.1/db?user=u&password=TOPSECRET1"}, "TOPSECRET1"),
        ({"default": {"db_url": "postgresql://u:TOPSECRET2@127.0.0.1/db"}}, "TOPSECRET2"),
        ({"default": {"engine": "e", "credentials": {"dsn": "postgresql://u:TOPSECRET3@h/db"}}}, "TOPSECRET3"),
        ({"default": {"engine": "e", "credentials": {"sslpassword": "TOPSECRET4"}}}, "TOPSECRET4"),
        ({"default": {"engine": "e", "credentials": {"server_settings": {"passwd": "TOPSECRET5"}}}}, "TOPSECRET5"),
        ({"default": "postgresql://127.0.0.1/db?sslpassword=TOPSECRET6&application_name=a"}, "TOPSECRET6"),
    ],
)
def test_star_password_masks_every_password_form(connections_config, secret):
    masked = ConfigSecrets.get_masked_connections(connections_config)
    assert secret not in masked
    assert "***" in masked


def test_star_password_keeps_everything_else():
    masked = ConfigSecrets.get_masked_connections(
        {"default": "postgresql://user:secretpassword@db.example/db?application_name=api"}
    )
    assert masked == "{'default': 'postgresql://user:***@db.example/db?application_name=api'}"


def test_star_password_reveals_no_part_of_the_password():
    masked = ConfigSecrets.get_masked_connections(
        {
            "url": "postgresql://user:secretpassword@db.example/db?sslpassword=secretsslpass",
            "credentials": {"engine": "e", "credentials": {"password": "secretpassword"}},
        }
    )
    assert "secr" not in masked
    assert masked.count("***") == 3


def test_connection_config_reprs_mask_every_secret():
    url_config = DBUrlConfig("postgresql://user:TOPSECRET1@db.example/db?sslpassword=TOPSECRET2&application_name=api")
    credentials_config = ConnectionConfig(
        engine="postgresql+asyncpg", credentials={"host": "db.example", "password": "TOPSECRET3"}
    )
    url_connection_config = ConnectionConfig(db_url="postgresql://user:TOPSECRET4@db.example/db")
    hare_config = HareConfig(
        connections={"url": url_config, "credentials": credentials_config},
        apps={"models": AppConfig(models=["tests.testmodels"])},
    )

    for rendered in (repr(url_config), repr(credentials_config), repr(url_connection_config), repr(hare_config)):
        assert "TOPSECRET" not in rendered
        assert "***" in rendered
    assert "application_name=api" in repr(url_config)
    assert "'host': 'db.example'" in repr(credentials_config)
    assert url_config.url.endswith("sslpassword=TOPSECRET2&application_name=api")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "build_config",
    [
        lambda: {
            "connections": {"default": "sqlite+aiosqlite://:memory:"},
            "apps": {"models": {"models": "tests.testmodels"}},
        },
        lambda: HareConfig.from_db_url("sqlite+aiosqlite://:memory:", {"models": "tests.testmodels"}),
    ],
)
async def test_models_given_as_a_single_string_are_refused(build_config):
    """A bare string was iterated character by character - 'Module "t" not found'."""
    async with HareContext() as ctx:
        with pytest.raises(ConfigurationError, match="must be a list/tuple of module paths, got str"):
            await ctx.init(build_config())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("engine", "message"),
    [
        ("hare.dialects.nosuch", 'Unknown database engine "hare.dialects.nosuch"'),
        ("", "Connection engine must be a non-empty driver name"),
    ],
)
async def test_unknown_engine_raises_configuration_error(engine, message):
    async with HareContext() as ctx:
        with pytest.raises(ConfigurationError, match=message):
            await ctx.connections._init({"default": {"engine": engine, "credentials": {}}}, False)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ({"use_timezone": "no"}, "use_timezone must be a bool"),
        ({"use_timezone": 1}, "use_timezone must be a bool"),
        ({"table_name_generator": "prefix_"}, "table_name_generator must be callable"),
    ],
)
async def test_init_keyword_arguments_are_type_checked(arguments, message):
    async with HareContext() as ctx:
        with pytest.raises(ConfigurationError, match=message):
            await ctx.init(config=MEMORY_CONFIG, **arguments)


@pytest.mark.asyncio
async def test_router_class_needing_constructor_arguments_is_a_configuration_error():
    class RouterWithArguments:
        def __init__(self, required_argument):
            self.required_argument = required_argument

    async with HareContext() as ctx:
        with pytest.raises(ConfigurationError, match="Can't instantiate router"):
            await ctx.init(config=MEMORY_CONFIG, routers=[RouterWithArguments])


@pytest.mark.asyncio
@pytest.mark.parametrize("threshold", ["abc", -5, float("nan"), float("inf"), True, 10**12, 10**400])
async def test_invalid_slow_query_threshold_is_refused_and_changes_nothing(threshold):
    Observers.slow_query_threshold_ms = SLOW_QUERY_THRESHOLD_MS
    with pytest.raises(ConfigurationError, match="slow_query_threshold_ms"):
        await Hare.init(config=MEMORY_CONFIG, slow_query_threshold_ms=threshold)
    assert Observers.slow_query_threshold_ms == SLOW_QUERY_THRESHOLD_MS
    assert HareContext.get_current() is None


@pytest.mark.asyncio
async def test_failed_init_leaves_process_wide_settings_unchanged():
    """Hare.init() set the slow-query threshold before validating the rest of the configuration -
    a failed init left it changed."""
    Observers.slow_query_threshold_ms = SLOW_QUERY_THRESHOLD_MS
    with pytest.raises(ConfigurationError):
        await Hare.init(
            HareConfig.from_db_url("sqlite+aiosqlite://:memory:", {"models": ["tests.no_such_models_module"]}),
            slow_query_threshold_ms=1.0,
            table_name_generator=lambda model: f"failed_{model.__name__.lower()}",
        )
    assert Observers.slow_query_threshold_ms == SLOW_QUERY_THRESHOLD_MS


def _make_generated_name_module() -> tuple[str, type[Model], type[Model]]:
    module_name = f"tests._generated_name_{uuid.uuid4().hex}"

    class GeneratedNameWidget(Model):
        id = fields.IntField(primary_key=True)
        name = fields.CharField(max_length=20)

    class ExplicitNameWidget(Model):
        id = fields.IntField(primary_key=True)

        class Meta:
            table = "explicit_widget_table"

    module = types.ModuleType(module_name)
    module.GeneratedNameWidget = GeneratedNameWidget  # type: ignore[attr-defined]
    module.ExplicitNameWidget = ExplicitNameWidget  # type: ignore[attr-defined]
    sys.modules[module_name] = module
    return module_name, GeneratedNameWidget, ExplicitNameWidget


@pytest.mark.asyncio
async def test_generated_table_name_is_regenerated_by_every_init():
    """The name the first init's table_name_generator produced used to stick to the model class
    for good - a later init with another generator, or none, still used it."""
    module_name, generated_model, explicit_model = _make_generated_name_module()
    try:
        async with HareContext() as ctx:
            await ctx.init(
                HareConfig.from_db_url("sqlite+aiosqlite://:memory:", {"models": [module_name]}),
                table_name_generator=lambda model: f"pfx_{model.__name__.lower()}",
            )
            assert generated_model._meta.db_table == "pfx_generatednamewidget"
            await ctx.generate_schemas()
            await generated_model.objects.create(name="first")

        async with HareContext() as ctx:
            await ctx.init(HareConfig.from_db_url("sqlite+aiosqlite://:memory:", {"models": [module_name]}))
            assert generated_model._meta.db_table == "generatednamewidget"
            assert explicit_model._meta.db_table == "explicit_widget_table"
            await ctx.generate_schemas()
            await generated_model.objects.create(name="second")
            assert await generated_model.objects.all().count() == 1
            rows = await ctx.get_connection().execute_dicts("SELECT name FROM generatednamewidget")
            assert rows == [{"name": "second"}]

        async with HareContext() as ctx:
            await ctx.init(
                HareConfig.from_db_url("sqlite+aiosqlite://:memory:", {"models": [module_name]}),
                table_name_generator=lambda model: f"other_{model.__name__.lower()}",
            )
            assert generated_model._meta.db_table == "other_generatednamewidget"
            assert explicit_model._meta.db_table == "explicit_widget_table"
    finally:
        sys.modules.pop(module_name, None)


@pytest.mark.asyncio
async def test_require_capability_defaults_to_the_contexts_own_connection():
    """requires_features() looked up a connection named "models", which hare_test_context()'s
    default ("default") never creates - its own docstring example failed."""
    ran = []

    @requires_features(supports_transactions=True)
    async def capability_checked_test():
        ran.append(True)

    async with hare_test_context(["tests.testmodels"]):
        await capability_checked_test()

    assert ran == [True]


def _make_cross_connection_modules(db_constraint: bool, relation: str) -> tuple[str, str]:
    suffix = uuid.uuid4().hex
    target_module_name = f"tests._xconn_target_{suffix}"
    source_module_name = f"tests._xconn_source_{suffix}"

    class XconnOwner(Model):
        id = fields.IntField(primary_key=True)

        class Meta:
            app = "xconn_target"

    if relation == "fk":

        class XconnPet(Model):
            id = fields.IntField(primary_key=True)
            owner = fields.ForeignKeyField(  # type: ignore[call-overload]
                "xconn_target.XconnOwner", related_name="pets", db_constraint=db_constraint
            )

            class Meta:
                app = "xconn_source"

    else:

        class XconnPet(Model):  # type: ignore[no-redef]
            id = fields.IntField(primary_key=True)
            owners = fields.ManyToManyField(  # type: ignore[call-overload]
                "xconn_target.XconnOwner", related_name="pets", db_constraint=db_constraint
            )

            class Meta:
                app = "xconn_source"

    target_module = types.ModuleType(target_module_name)
    target_module.XconnOwner = XconnOwner  # type: ignore[attr-defined]
    source_module = types.ModuleType(source_module_name)
    source_module.XconnPet = XconnPet  # type: ignore[attr-defined]
    sys.modules[target_module_name] = target_module
    sys.modules[source_module_name] = source_module
    return target_module_name, source_module_name


def _cross_connection_config(target_module_name: str, source_module_name: str) -> dict:
    db_url = os.getenv("HARE_TEST_DB", "sqlite+aiosqlite://:memory:")
    return {
        "connections": {
            "one": DbUrlConfigGenerator.expand(db_url, testing=True),
            "two": DbUrlConfigGenerator.expand(db_url, testing=True),
        },
        "apps": {
            "xconn_target": {"models": [target_module_name], "default_connection": "one"},
            "xconn_source": {"models": [source_module_name], "default_connection": "two"},
        },
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("relation", ["fk", "m2m"])
async def test_database_constraint_across_connections_is_refused_at_init(relation):
    """A db_constraint=True relation to a model on another connection used to pass init and fail
    later - Postgres generate_schemas() with 'relation "owner" does not exist', SQLite with a
    dangling FK and 'no such table' on the first insert."""
    target_module_name, source_module_name = _make_cross_connection_modules(True, relation)
    try:
        async with HareContext() as ctx:
            with pytest.raises(ConfigurationError, match="db_constraint=False"):
                await ctx.init(config=_cross_connection_config(target_module_name, source_module_name))
    finally:
        sys.modules.pop(target_module_name, None)
        sys.modules.pop(source_module_name, None)


@pytest.mark.asyncio
async def test_relation_across_connections_without_database_constraint_still_works():
    target_module_name, source_module_name = _make_cross_connection_modules(False, "fk")
    owner_model = sys.modules[target_module_name].XconnOwner
    pet_model = sys.modules[source_module_name].XconnPet
    try:
        async with HareContext() as ctx:
            await ctx.init(config=_cross_connection_config(target_module_name, source_module_name), _create_db=True)
            try:
                await ctx.generate_schemas()
                owner = await owner_model.objects.create()
                await pet_model.objects.create(owner=owner)
                assert await owner.pets.all().count() == 1
                await owner.delete()
                assert await pet_model.objects.all().count() == 0
            finally:
                await ctx.connections.close_all(discard=False)
                for connection in ctx.connections.all():
                    await connection.db_delete()
    finally:
        sys.modules.pop(target_module_name, None)
        sys.modules.pop(source_module_name, None)


@pytest.mark.parametrize(
    ("db_url", "expected_host"),
    [
        ("postgresql://user@%2Fvar%2Frun%2Fpostgresql/db", "/var/run/postgresql"),
        ("postgresql+asyncpg://user@%2Fvar%2Frun%2FPostgres/db", "/var/run/Postgres"),
        ("postgresql://user:pass@[fe80::1%25eth0]:5433/db", "fe80::1%eth0"),
        ("postgresql://user:pass@db.example:5433/db", "db.example"),
    ],
)
def test_db_url_host_is_percent_decoded(db_url, expected_host):
    assert DbUrlConfigGenerator.expand(db_url)["credentials"]["host"] == expected_host


def test_sqlite_db_url_path_is_percent_decoded():
    assert (
        DbUrlConfigGenerator.expand("sqlite+aiosqlite://my%20db.sqlite")["credentials"]["file_path"] == "my db.sqlite"
    )


@pytest.mark.parametrize(
    "db_url",
    [
        "postgresql://user:pass@localhost:5432/db?port=70000",
        "postgresql+asyncpg://user:pass@localhost/db?host=other",
        "postgresql+asyncpg://user:pass@localhost/db?password=other",
    ],
)
def test_db_url_setting_a_value_both_in_the_address_and_the_query_is_refused(db_url):
    """?port=70000 next to :5432 used to be dropped silently in favour of the address."""
    with pytest.raises(ConfigurationError, match="twice"):
        DbUrlConfigGenerator.expand(db_url)


@pytest.mark.asyncio
async def test_db_url_port_zero_is_refused():
    async with HareContext() as ctx:
        with pytest.raises(ConfigurationError, match="port must be between 1 and 65535"):
            await ctx.init(
                HareConfig.from_db_url(
                    "postgresql+asyncpg://user:pass@localhost:0/db", {"models": ["tests.testmodels"]}
                )
            )


@pytest.mark.asyncio
async def test_config_given_as_a_read_only_mapping_works():
    config = types.MappingProxyType(
        {
            "connections": {"default": "sqlite+aiosqlite://:memory:"},
            "apps": {"models": {"models": ["tests.testmodels"]}},
        }
    )
    async with HareContext() as context:
        await context.init(config=config)
        assert context.default_connection == "default"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("init_kwargs", "message"),
    [
        ({"config": ["not", "a", "mapping"]}, "config must be a HareConfig, a dict, a file path"),
        ({"config": MEMORY_CONFIG, "routers": "a.Router"}, r"routers must be a list .* write \['a.Router'\] instead"),
    ],
)
async def test_config_argument_of_the_wrong_shape_is_a_configuration_error(init_kwargs, message):
    async with HareContext() as context:
        with pytest.raises(ConfigurationError, match=message):
            await context.init(**init_kwargs)


def test_config_file_extension_is_case_insensitive(tmp_path):
    config_file = tmp_path / "HARE.JSON"
    config_file.write_text(
        '{"connections": {"default": "sqlite+aiosqlite://:memory:"}, "apps": {"models": {"models": ["m"]}}}'
    )

    assert HareConfig.from_config_file(str(config_file)).apps["models"].models == ["m"]
