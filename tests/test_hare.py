"""Tests for the ``Hare`` class's own API surface (as opposed to HareContext, covered by
test_context.py)."""

import pytest

from hare import Hare, fields
from hare.core.apps import Apps
from hare.core.config import ConfigSecrets, HareConfig
from hare.core.context import HareContext
from hare.exceptions import ConfigurationError
from hare.models import Model
from hare.models.deletion.deletion_graph import DeletionGraph


@pytest.mark.asyncio
async def test_close_connections_resets_inited_flag():
    """Hare.close_connections() must leave is_inited() reporting False, matching
    HareContext.__aexit__ - otherwise code that checks is_inited() to decide whether to
    (re)initialize sees a stale True even though the connections it would have used are
    already closed."""
    async with HareContext() as ctx:
        await Hare.init(HareConfig.from_db_url("sqlite://:memory:", {"models": ["tests.testmodels"]}))
        assert Hare.is_inited() is True

        await Hare.close_connections()

        assert Hare.is_inited() is False
        assert ctx.inited is False


@pytest.mark.asyncio
async def test_init_failure_does_not_leave_a_phantom_context_registered():
    """Hare.init() creates and registers its own fresh HareContext (via a bare __enter__(), not
    `async with`) when none is already current - without undoing that registration on a failed
    ctx.init(), HareContext.get_current() kept returning this never-successfully-inited context
    forever instead of None, and a later Hare.init() call would silently reuse it instead of
    starting clean."""
    assert HareContext.get_current() is None

    with pytest.raises(ConfigurationError):
        await Hare.init(HareConfig.from_db_url("sqlite://:memory:", {"models": ["tests.this_module_does_not_exist"]}))

    assert HareContext.get_current() is None


@pytest.mark.asyncio
async def test_register_live_model_single_call_replaces_manual_steps():
    """Hare.register_live_model() must let a Model class built AFTER Hare.init() already ran -
    e.g. one generated from live database introspection - reach a working .create()/.get() with
    ONE public call, instead of the three manual steps this used to take: ctx.apps.init_app(),
    setting model._meta.default_connection, and calling the private
    ctx.apps._build_initial_querysets() (skipping either of the latter two used to raise
    ConfigurationError/AttributeError on the model's first .save())."""
    async with HareContext() as ctx:
        await Hare.init(HareConfig.from_db_url("sqlite://:memory:", {"models": ["tests.testmodels"]}))

        # Built here, at test-runtime, not at module import time - the whole point of the
        # feature is registering a model hare-orm didn't know about at Hare.init() time.
        class LiveWidget(Model):
            id = fields.IntField(primary_key=True)
            name = fields.CharField(max_length=32)

        Hare.register_live_models([LiveWidget], app_label="dynamic")

        assert LiveWidget._meta.default_connection == "default"
        assert ctx.apps["dynamic"]["LiveWidget"] is LiveWidget

        await LiveWidget._meta.db.execute_script(
            f'CREATE TABLE "{LiveWidget._meta.db_table}" (id INTEGER PRIMARY KEY, name TEXT)'
        )
        created = await LiveWidget.objects.create(name="from-register-live-model")
        fetched = await LiveWidget.objects.get(id=created.id)
        assert fetched.name == "from-register-live-model"


@pytest.mark.asyncio
async def test_register_live_model_requires_active_context():
    class Orphan(Model):
        id = fields.IntField(primary_key=True)

    with pytest.raises(ConfigurationError, match="not initialized"):
        Hare.register_live_models([Orphan], app_label="dynamic")


@pytest.mark.asyncio
async def test_register_live_model_rejects_unknown_connection_alias():
    async with HareContext():
        await Hare.init(HareConfig.from_db_url("sqlite://:memory:", {"models": ["tests.testmodels"]}))

        class Orphan(Model):
            id = fields.IntField(primary_key=True)

        with pytest.raises(ConfigurationError, match='Unknown connection "not_a_real_alias"'):
            Hare.register_live_models([Orphan], app_label="dynamic", connection_alias="not_a_real_alias")


@pytest.mark.asyncio
async def test_register_live_model_rejects_table_name_collision():
    """A table-name collision with an already-registered model must raise a clear
    ConfigurationError at registration time - not surface later as a confusing raw driver error
    once schema creation/queries actually hit the database."""
    async with HareContext():
        await Hare.init(HareConfig.from_db_url("sqlite://:memory:", {"models": ["tests.testmodels"]}))

        class Tournament(Model):
            id = fields.IntField(primary_key=True)

        with pytest.raises(ConfigurationError, match="both resolve to table"):
            Hare.register_live_models([Tournament], app_label="dynamic")


@pytest.mark.asyncio
async def test_register_live_model_rejects_table_name_collision_under_same_app_label():
    """The same table-name collision as test_register_live_model_rejects_table_name_collision,
    but between two models registered under the SAME app_label via two SEPARATE
    register_live_model() calls - Apps.init_app() used to replace `self.apps[label]` wholesale on
    every call, so by the time the second call's _init_relations() ran its collision check, the
    FIRST model was already evicted from self.apps and the collision went undetected entirely.
    Apps.init_app() now merges into whatever `label` already holds instead of replacing it, so
    the first model stays visible to the second call's own collision check."""
    async with HareContext():
        await Hare.init(HareConfig.from_db_url("sqlite://:memory:", {"models": ["tests.testmodels"]}))

        class FirstLiveModel(Model):
            id = fields.IntField(primary_key=True)

            class Meta:
                table = "shared_live_table"

        class SecondLiveModel(Model):
            id = fields.IntField(primary_key=True)

            class Meta:
                table = "shared_live_table"

        Hare.register_live_models([FirstLiveModel], app_label="dynamic")

        with pytest.raises(ConfigurationError, match="both resolve to table"):
            Hare.register_live_models([SecondLiveModel], app_label="dynamic")


@pytest.mark.asyncio
async def test_register_live_model_reregistering_same_class_under_same_app_label_is_not_a_collision():
    """The merge fix behind test_register_live_model_rejects_table_name_collision_under_same_
    app_label must not turn a legitimate RE-registration (the SAME class, same app_label - e.g.
    calling register_live_model() again after some unrelated re-init) into a false-positive
    collision: the class overwrites itself under the SAME dict key, so only one model ever
    occupies that table name from Apps's own point of view."""
    async with HareContext() as ctx:
        await Hare.init(HareConfig.from_db_url("sqlite://:memory:", {"models": ["tests.testmodels"]}))

        class LiveWidget(Model):
            id = fields.IntField(primary_key=True)

            class Meta:
                table = "reregistered_live_table"

        Hare.register_live_models([LiveWidget], app_label="dynamic")
        Hare.register_live_models([LiveWidget], app_label="dynamic")

        assert ctx.apps["dynamic"]["LiveWidget"] is LiveWidget


@pytest.mark.asyncio
async def test_executor_cache_does_not_leak_between_different_classes_sharing_a_table():
    """BaseExecutor.EXECUTOR_CACHE used to be keyed by (connection_name, dialect, schema,
    db_table) ONLY, with no reference to the model class itself - regular_columns/insert_query/
    update_cache/etc, every one of them a pure function of the MODEL's own field set, not just
    its table name. Two DIFFERENT classes sharing that same 4-tuple - the real,
    register_live_model()-reload-after-a-schema-change scenario this reproduces, since there is
    no dedicated "unregister"/"reload" API (a caller works around that, as this test does, by
    removing the old app registration directly before registering a fresh one for the same
    table) - used to silently reuse the FIRST class's cached insert_query for the SECOND: any
    column the first class's own fields didn't have was silently dropped from every INSERT the
    second class ever issued, with zero error at all."""
    async with HareContext() as ctx:
        await Hare.init(HareConfig.from_db_url("sqlite://:memory:", {"models": ["tests.testmodels"]}))
        connection = ctx.connections.get("default")
        await connection.execute_script(
            'CREATE TABLE "executor_cache_reload_widget" (id INTEGER PRIMARY KEY, name TEXT, extra_note TEXT)'
        )

        class ReloadWidgetV1(Model):
            id = fields.IntField(primary_key=True)
            name = fields.CharField(max_length=64)

            class Meta:
                managed = False
                table = "executor_cache_reload_widget"

        Hare.register_live_models([ReloadWidgetV1], app_label="reload_v1")
        await ReloadWidgetV1.objects.create(name="A")

        del ctx.apps.apps["reload_v1"]

        class ReloadWidgetV2(Model):
            id = fields.IntField(primary_key=True)
            name = fields.CharField(max_length=64)
            extra_note = fields.CharField(max_length=64, null=True)

            class Meta:
                managed = False
                table = "executor_cache_reload_widget"

        Hare.register_live_models([ReloadWidgetV2], app_label="reload_v2")
        await ReloadWidgetV2.objects.create(name="B", extra_note="note-b")

        rows = {(w.name, w.extra_note) for w in await ReloadWidgetV2.objects.all()}
        assert ("B", "note-b") in rows, (
            "extra_note was silently dropped from the INSERT - EXECUTOR_CACHE reused the first "
            "model class's cached insert_query for a structurally different second class sharing "
            "the same table"
        )


@pytest.mark.asyncio
async def test_unregister_requires_active_context():
    class Orphan(Model):
        id = fields.IntField(primary_key=True)

    with pytest.raises(ConfigurationError, match="not initialized"):
        Hare.unregister_live_models([Orphan])


@pytest.mark.asyncio
async def test_register_live_model_sets_managed_before_relations_are_initialised(monkeypatch):
    async with HareContext() as ctx:
        await Hare.init(HareConfig.from_db_url("sqlite://:memory:", {"models": ["tests.testmodels"]}))

        class UnmanagedWidget(Model):
            id = fields.IntField(primary_key=True)

        class ManagedWidget(Model):
            id = fields.IntField(primary_key=True)

        managed_seen_by_init_relations: list[tuple[str, bool]] = []
        original_init_relations = Apps._init_relations

        def recording_init_relations(apps: Apps) -> None:
            for model in (UnmanagedWidget, ManagedWidget):
                if model._meta.app == "dynamic":
                    managed_seen_by_init_relations.append((model.__name__, model._meta.managed))
            original_init_relations(apps)

        monkeypatch.setattr(Apps, "_init_relations", recording_init_relations)
        Hare.register_live_models([UnmanagedWidget], app_label="dynamic", managed=False)
        Hare.register_live_models([ManagedWidget], app_label="dynamic")

        assert ("UnmanagedWidget", False) in managed_seen_by_init_relations
        assert ("ManagedWidget", True) in managed_seen_by_init_relations
        assert ("UnmanagedWidget", True) not in managed_seen_by_init_relations
        assert UnmanagedWidget._meta.managed is False
        assert ManagedWidget._meta.managed is True
        assert ctx.apps["dynamic"]["UnmanagedWidget"] is UnmanagedWidget


@pytest.mark.asyncio
async def test_registering_protect_relation_invalidates_transitive_protect_cache():
    """Regression: MetaInfo.add_field() only evicted the direct target's backward-relation cache,
    so has_transitive_protect() on a model further up a CASCADE chain kept its cached False after
    a live model added a PROTECT relation below it - hard deletes then skipped the transitive
    PROTECT check."""
    async with HareContext():
        await Hare.init(HareConfig.from_db_url("sqlite://:memory:", {"models": ["tests.testmodels"]}))

        class ChainTop(Model):
            id = fields.IntField(primary_key=True)

        class ChainMiddle(Model):
            id = fields.IntField(primary_key=True)
            top = fields.ForeignKeyField("chain.ChainTop", related_name="middles")

        Hare.register_live_models([ChainTop], app_label="chain")
        Hare.register_live_models([ChainMiddle], app_label="chain")
        assert DeletionGraph.has_transitive_protect(ChainTop) is False

        class ChainGuard(Model):
            id = fields.IntField(primary_key=True)
            middle = fields.ForeignKeyField("chain.ChainMiddle", related_name="guards", on_delete=fields.PROTECT)

        Hare.register_live_models([ChainGuard], app_label="chain")
        assert DeletionGraph.has_transitive_protect(ChainTop) is True

        Hare.unregister_live_models([ChainGuard])
        assert DeletionGraph.has_transitive_protect(ChainTop) is False


@pytest.mark.asyncio
async def test_register_live_model_managed_none_keeps_meta_managed():
    """managed= left at its default must not override a Meta.managed declared on the model
    itself; an explicit value wins over it."""
    async with HareContext():
        await Hare.init(HareConfig.from_db_url("sqlite://:memory:", {"models": ["tests.testmodels"]}))

        class DeclaredUnmanaged(Model):
            id = fields.IntField(primary_key=True)

            class Meta:
                managed = False

        class DeclaredUnmanagedForcedManaged(Model):
            id = fields.IntField(primary_key=True)

            class Meta:
                managed = False

        Hare.register_live_models([DeclaredUnmanaged], app_label="dynamic")
        Hare.register_live_models([DeclaredUnmanagedForcedManaged], app_label="dynamic", managed=True)

        assert DeclaredUnmanaged._meta.managed is False
        assert DeclaredUnmanagedForcedManaged._meta.managed is True


@pytest.mark.parametrize(
    ("connections_config", "secret"),
    [
        ({"default": "postgresql://user:S3cr%40tPassw0rd@localhost/db"}, "tPassw0rd"),
        ({"default": "postgresql://user:pa/ss@wordzz@localhost/db"}, "ss@wordzz"),
        (
            {
                "default": {
                    "engine": "postgresql+asyncpg",
                    "credentials": {"password": "ab\\cdefghi"},
                }
            },
            "defghi",
        ),
        (
            {"default": {"engine": "postgresql+asyncpg", "credentials": {"password": 123456789}}},
            "456789",
        ),
    ],
)
def test_star_password_masks_every_password_shape(connections_config, secret):
    """A percent-encoded URL password leaked verbatim, a password with a backslash was not found in
    the repr() output, and a non-string password raised TypeError."""
    masked = ConfigSecrets.get_masked_connections(connections_config)

    assert secret not in masked
    assert "***" in masked


def test_star_password_keeps_url_without_password_unchanged():
    assert ConfigSecrets.get_masked_connections({"default": "sqlite://:memory:"}) == "{'default': 'sqlite://:memory:'}"


@pytest.mark.asyncio
@pytest.mark.parametrize("timezone", ["Mars/Olympus", "", "../etc/passwd"])
async def test_init_rejects_unknown_timezone(timezone):
    """An unknown timezone used to be accepted by init() and fail only on the first datetime use."""
    with pytest.raises(ConfigurationError, match="timezone"):
        await Hare.init(
            HareConfig.from_db_url("sqlite://:memory:", {"models": ["tests.testmodels"]}), timezone=timezone
        )

    assert HareContext.get_current() is None


@pytest.mark.asyncio
async def test_init_accepts_case_insensitive_known_timezone():
    async with HareContext() as ctx:
        await Hare.init(
            HareConfig.from_db_url("sqlite://:memory:", {"models": ["tests.testmodels"]}), timezone="europe/moscow"
        )
        assert ctx.timezone == "europe/moscow"
        await Hare.close_connections()


def _prefixed_table_name(model: type[Model]) -> str:
    return f"pfx_{model.__name__.lower()}"


@pytest.mark.asyncio
async def test_register_live_model_uses_its_own_contexts_table_name_generator():
    """register_live_model() used to take the table name generator from the Hare class - i.e.
    from whichever Hare.init() ran last, in any context - instead of from its own context."""
    prefixed_context = HareContext()
    with prefixed_context:
        await Hare.init(
            HareConfig.from_db_url("sqlite://:memory:", {"models": ["tests.testmodels"]}),
            table_name_generator=_prefixed_table_name,
        )
    unprefixed_context = HareContext()
    with unprefixed_context:
        await Hare.init(HareConfig.from_db_url("sqlite://:memory:", {"models": ["tests.testmodels"]}))
    try:
        with prefixed_context:

            class LiveGeneratedName(Model):
                id = fields.IntField(primary_key=True)

            Hare.register_live_models([LiveGeneratedName], app_label="dynamic")
            assert LiveGeneratedName._meta.db_table == "pfx_livegeneratedname"
    finally:
        await prefixed_context.close_connections()
        await unprefixed_context.close_connections()


@pytest.mark.asyncio
async def test_register_live_model_uses_table_name_generator_of_a_directly_inited_context():
    async with HareContext() as ctx:
        await ctx.init(
            HareConfig.from_db_url("sqlite://:memory:", {"models": ["tests.testmodels"]}),
            table_name_generator=_prefixed_table_name,
        )

        class LiveDirectGeneratedName(Model):
            id = fields.IntField(primary_key=True)

        Hare.register_live_models([LiveDirectGeneratedName], app_label="dynamic")
        assert LiveDirectGeneratedName._meta.db_table == "pfx_livedirectgeneratedname"
