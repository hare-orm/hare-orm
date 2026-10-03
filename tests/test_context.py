"""
Tests for hare.core.context module - HareContext class.

These tests verify the context-based state management for Hare ORM.

Note: These tests may run with a session-scoped default context active
(created by Hare.init() in conftest.py). The tests account for this
by testing context isolation relative to the current state.
"""

import asyncio
import contextlib
import json

import pytest
import yaml

import tests.testmodels as testmodels_module
from hare.contrib.test.helpers import hare_test_context
from hare.core.caches import Caches
from hare.core.config import AppConfig, DBUrlConfig, HareConfig
from hare.core.connection_handler import ConnectionHandler
from hare.core.context import HareContext
from hare.exceptions import ConfigurationError
from hare.instrumentation.observer_set import ObserverSet
from hare.instrumentation.observers import Observers
from hare.instrumentation.query_executed import QueryExecuted
from hare.instrumentation.transaction_event import TransactionEvent
from hare.transactions.enums import TransactionEventType
from hare.utils import Timezone


def _clear_global_context():
    """Clear the global context to ensure test isolation."""
    import hare.core.context as ctx_module

    ctx_module.HareContext.global_context = None


@pytest.fixture(autouse=True)
def cleanup_global_context():
    """Fixture to clear global context before and after each test."""
    _clear_global_context()
    yield
    _clear_global_context()


class TestHareContextInstantiation:
    """Test cases for HareContext instantiation."""

    def test_context_instantiation_initial_state(self):
        """HareContext instantiation has correct initial state."""
        ctx = HareContext()

        assert ctx._connections is None
        assert ctx._apps is None
        assert ctx._inited is False
        assert ctx.inited is False

    def test_context_connections_property_lazy_creation(self):
        """ConnectionHandler is lazily created on first access."""
        ctx = HareContext()

        # Before access
        assert ctx._connections is None

        # After access
        connections = ctx.connections
        assert ctx._connections is not None
        assert isinstance(connections, ConnectionHandler)

    def test_context_apps_property_initially_none(self):
        """Apps property is initially None."""
        ctx = HareContext()

        assert ctx.apps is None


class TestContextManagerProtocol:
    """Test cases for context manager protocol."""

    def test_context_manager_sets_current_context(self):
        """Context manager sets current context."""
        # Save original context (may be session-scoped default)
        original_ctx = HareContext.get_current()

        with HareContext() as ctx:
            assert HareContext.get_current() is ctx

        # After exit, should return to original
        assert HareContext.get_current() is original_ctx

    def test_context_manager_resets_on_exit(self):
        """Context manager resets on exit."""
        original_ctx = HareContext.get_current()

        with HareContext():
            pass

        assert HareContext.get_current() is original_ctx

    def test_nested_contexts_work_correctly(self):
        """Nested contexts work correctly."""
        original_ctx = HareContext.get_current()

        with HareContext() as outer:
            assert HareContext.get_current() is outer

            with HareContext() as inner:
                assert HareContext.get_current() is inner

            # After inner exits, should return to outer
            assert HareContext.get_current() is outer

        # After all exit, should return to original
        assert HareContext.get_current() is original_ctx


class TestRequireContext:
    """Test cases for require_context function."""

    def test_require_context_raises_when_no_context(self):
        """require_context raises when no context is active.

        Note: With the new architecture, Hare.init() creates a default context,
        so this test only passes when run in complete isolation. When a session-scoped
        context exists, HareContext.require_current() returns it instead of raising.
        """
        # This behavior depends on whether a session context exists
        original_ctx = HareContext.get_current()
        if original_ctx is not None:
            # Session context exists, require_context should return it
            result = HareContext.require_current()
            assert result is original_ctx
        else:
            # No session context, should raise
            with pytest.raises(ConfigurationError, match="Hare ORM is not initialized"):
                HareContext.require_current()

    def test_require_context_returns_active_context(self):
        """require_context returns the active context when one exists."""
        with HareContext() as ctx:
            result = HareContext.require_current()
            assert result is ctx


class TestConnectionHandlerIsolation:
    """Test cases for ConnectionHandler isolation."""

    def test_each_context_gets_own_connection_handler(self):
        """Each context gets own ConnectionHandler."""
        ctx1 = HareContext()
        ctx2 = HareContext()

        # Access connections property on both
        conn1 = ctx1.connections
        conn2 = ctx2.connections

        # Should be different instances
        assert conn1 is not conn2

    def test_context_connections_isolated_from_global(self):
        """Context connections isolated from global."""
        ctx = HareContext()

        # Context's ConnectionHandler should be completely independent
        # It should not have any config yet
        with pytest.raises(ConfigurationError):
            ctx.connections.db_config


class TestAsyncContextManager:
    """Test cases for async context manager protocol."""

    @pytest.mark.asyncio
    async def test_async_context_manager_sets_current_context(self):
        """Async context manager sets current context."""
        original_ctx = HareContext.get_current()

        async with HareContext() as ctx:
            assert HareContext.get_current() is ctx

        # After exit, should return to original
        assert HareContext.get_current() is original_ctx

    @pytest.mark.asyncio
    async def test_async_context_manager_resets_on_exit(self):
        """Async context manager resets on exit."""
        original_ctx = HareContext.get_current()

        async with HareContext():
            pass

        assert HareContext.get_current() is original_ctx

    @pytest.mark.asyncio
    async def test_connections_cleaned_on_async_context_exit(self):
        """Connections closed on async context exit."""
        ctx = HareContext()

        # Access connections to create the handler
        _ = ctx.connections
        assert ctx._connections is not None

        async with ctx:
            pass

        # After exit, connections should be cleaned up
        assert ctx._connections is None

    @pytest.mark.asyncio
    async def test_apps_cleared_on_async_context_exit(self):
        """Apps cleared on context exit."""
        ctx = HareContext()

        async with ctx:
            # Manually set apps to simulate initialization
            ctx._apps = {}
            ctx._inited = True

        # After exit, apps should be cleared
        assert ctx.apps is None
        assert ctx.inited is False

    def test_set_global_context_sets_global(self):
        """set_global_context sets the global context."""
        ctx = HareContext()
        HareContext.set_global(ctx)

        # Verify global context is set
        import hare.core.context as ctx_module

        assert ctx_module.HareContext.global_context is ctx

    def test_set_global_context_raises_when_already_set(self):
        """set_global_context raises ConfigurationError when already set."""
        ctx1 = HareContext()
        ctx2 = HareContext()

        HareContext.set_global(ctx1)

        with pytest.raises(ConfigurationError):
            HareContext.set_global(ctx2)

    def test_set_global_context_again_for_the_same_context(self):
        """Setting the context that already is the global one again changes nothing."""
        ctx = HareContext()
        HareContext.set_global(ctx)
        HareContext.set_global(ctx)

        assert HareContext.global_context is ctx

    @pytest.mark.asyncio
    async def test_reinit_of_the_global_context_with_global_fallback(self):
        """A re-init of the global context keeping _enable_global_fallback=True succeeds and
        closes the connections it replaces."""
        from tests.testmodels import Tournament

        async with HareContext() as ctx:
            await ctx.init(
                HareConfig.from_db_url("sqlite://:memory:", {"models": ["tests.testmodels"]}),
                _enable_global_fallback=True,
            )
            previous_connections = ctx.connections
            await ctx.init(
                HareConfig.from_db_url("sqlite://:memory:", {"models": ["tests.testmodels"]}),
                _enable_global_fallback=True,
            )
            await ctx.generate_schemas()

            assert HareContext.global_context is ctx
            assert ctx.connections is not previous_connections
            assert previous_connections._get_storage() == {}
            assert await Tournament.objects.all().count() == 0

    def test_bind_models_failure_leaves_no_context_current(self):
        """Hare.bind_models() with no context current leaves the context it created again when it
        fails."""
        import contextvars

        from hare import Hare

        def bind_models_in_empty_context():
            with pytest.raises(ConfigurationError, match='Module "tests.no_such_models_module" not found'):
                Hare.bind_models(
                    HareConfig.from_db_url("sqlite://:memory:", {"models": ["tests.no_such_models_module"]})
                )
            return HareContext.get_current()

        assert contextvars.Context().run(bind_models_in_empty_context) is None


class TestCloseConnectionsExceptionSafety:
    """close_connections() must reset this context's state even when the underlying
    close_all() raises - a broken connection's close() failing must not permanently wedge
    the context in an inited-but-torn-down limbo."""

    @pytest.mark.asyncio
    async def test_close_connections_resets_state_when_close_all_raises(self, monkeypatch):
        ctx = HareContext()
        async with ctx:
            await ctx.init(HareConfig.from_db_url("sqlite://:memory:", {"models": ["tests.testmodels"]}))
            conn = ctx.connections.get("default")

            async def broken_close():
                raise RuntimeError("close is broken")

            monkeypatch.setattr(conn, "close", broken_close)

            with pytest.raises(RuntimeError, match="close is broken"):
                await ctx.close_connections()

            assert ctx.inited is False
            assert ctx._connections is None

            # A fresh init() afterward must work cleanly, not stay permanently wedged.
            await ctx.init(HareConfig.from_db_url("sqlite://:memory:", {"models": ["tests.testmodels"]}))
            assert ctx.inited is True


class TestAexitExceptionSafety:
    """__aexit__() must still reset the "current context" contextvar and unregister every hook
    registered while this context was active, even when close_connections() itself raises - a
    connection that fails to close must not leave this context wedged as "current" forever, or
    keep its observers getting events."""

    @pytest.mark.asyncio
    async def test_aexit_restores_current_context_and_unregisters_hooks_when_close_fails(self, monkeypatch):
        outer = HareContext.get_current()
        ctx = HareContext()

        def hook(event, **kwargs):
            pass

        with pytest.raises(RuntimeError, match="close is broken"):
            async with ctx:
                await ctx.init(HareConfig.from_db_url("sqlite://:memory:", {"models": ["tests.testmodels"]}))
                ctx.observe(QueryExecuted, hook)
                assert ctx.observers.observes(QueryExecuted)

                conn = ctx.connections.get("default")

                async def broken_close():
                    raise RuntimeError("close is broken")

                monkeypatch.setattr(conn, "close", broken_close)

        # The contextvar must be restored to whatever was active before ctx, not left pointing
        # at this now-torn-down context.
        assert HareContext.get_current() is outer
        # The observer registered on ctx is dropped, not leaked.
        assert not ctx.observers.observes(QueryExecuted)


class TestConcurrentSharedInstanceReentry:
    """Sharing one HareContext instance across concurrently-running asyncio tasks must fail
    loudly and immediately at the second __aenter__(), instead of overwriting self._token and
    later crashing __aexit__() with a confusing 'Token ... was created in a different Context'
    ValueError that masks the first task's own exception/clean completion."""

    @pytest.mark.asyncio
    async def test_concurrent_entry_of_same_instance_raises_configuration_error(self):
        shared_ctx = HareContext()
        entered_first = asyncio.Event()
        release_first = asyncio.Event()

        async def task_a():
            async with shared_ctx:
                entered_first.set()
                await release_first.wait()

        async def task_b():
            await entered_first.wait()
            with pytest.raises(ConfigurationError, match="already active"):
                async with shared_ctx:
                    pass
            release_first.set()

        # task_a must complete cleanly (no exception raised out of its __aexit__) - if the second
        # entry were allowed to corrupt self._token, task_a's own __aexit__ would blow up instead.
        await asyncio.gather(task_a(), task_b())


class TestGetModel:
    """Test cases for get_model method."""

    def test_get_model_raises_when_not_initialized(self):
        """get_model raises when not initialized."""
        ctx = HareContext()

        with pytest.raises(ConfigurationError) as exc_info:
            ctx.get_model("models", "User")

        assert "Context not initialized" in str(exc_info.value)


class TestInit:
    """Test cases for init method."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "models",
        [
            pytest.param(("tests.testmodels",), id="tuple_of_str"),
            pytest.param((testmodels_module,), id="tuple_of_module_type"),
            pytest.param([testmodels_module], id="list_of_module_type"),
        ],
    )
    async def test_init_discovers_models_for_every_documented_modules_form(self, models):
        """`modules=` is documented (dict[str, Iterable[str | ModuleType]]) to accept any
        iterable of dotted-path strings or already-imported modules, not just a list of
        strings - and the model it names must actually be discovered/queryable afterward, not
        merely avoid raising."""
        async with HareContext() as ctx:
            await ctx.init(HareConfig.from_db_url("sqlite://:memory:", {"models": models}))

            assert ctx.get_model("models", "Tournament") is not None

    @pytest.mark.asyncio
    async def test_init_before_the_context_is_entered(self):
        """init() sets the context up even when it isn't the current one yet - entering it
        afterwards runs queries on it, and the context current before stays current meanwhile."""
        from tests.testmodels import Tournament

        previous = HareContext.get_current()
        ctx = HareContext()
        await ctx.init(HareConfig.from_db_url("sqlite://:memory:", {"models": ["tests.testmodels"]}))
        assert HareContext.get_current() is previous
        async with ctx:
            await ctx.generate_schemas()
            await Tournament.objects.create(name="entered later")
            assert await Tournament.objects.filter(name="entered later").count() == 1

    @pytest.mark.asyncio
    async def test_init_raises_with_invalid_config_no_connections(self):
        """init() raises when config missing connections section."""
        ctx = HareContext()

        with pytest.raises(ConfigurationError) as exc_info:
            await ctx.init(config={"apps": {}})

        assert 'Config must define "connections" section' in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_init_raises_with_invalid_config_no_apps(self):
        """init() raises when config missing apps section."""
        ctx = HareContext()

        with pytest.raises(ConfigurationError) as exc_info:
            await ctx.init(config={"connections": {}})

        assert 'Config must define "apps" section' in str(exc_info.value)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("serializer", [json.dumps, yaml.dump])
    async def test_init_with_config_file(self, tmp_path, serializer):
        """init() with config_file (JSON/YAML) initializes context correctly."""
        suffix = ".yaml" if serializer is yaml.dump else ".json"
        config_file = tmp_path / f"config{suffix}"

        config = HareConfig(
            connections={"default": DBUrlConfig("sqlite://:memory:")},
            apps={"models": AppConfig(models=["tests.testmodels"])},
        )
        config_dict = config.to_dict()
        config_file.write_text(serializer(config_dict))

        async with HareContext() as ctx:
            await ctx.init(str(config_file))

            assert ctx.inited is True
            assert ctx._connections is not None
            conn = ctx.connections.get("default")
            assert conn is not None
            assert ctx.apps is not None

    @pytest.mark.asyncio
    async def test_init_raises_with_invalid_config_file_extension(self, tmp_path):
        """init() raises when config_file has unsupported extension."""
        config_file = tmp_path / "config.txt"
        config_file.write_text("some config")

        ctx = HareContext()

        with pytest.raises(ConfigurationError) as exc_info:
            await ctx.init(str(config_file))

        assert "Unknown config extension .txt" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_retry_after_partial_init_failure_does_not_carry_forward_stale_alias(self):
        """A first init() call with one bad alias failing to even construct (an unknown DB
        scheme) used to leave that alias merged into the ConnectionHandler's own db_config -
        _init_config() does a dict.update(), not a replace, so a retried init() with a
        completely different, FIXED config (the bad alias removed entirely) still carried the
        stale, broken alias forward via that merge and failed again for the exact same reason,
        even though the caller's new config never mentioned it."""
        async with HareContext() as ctx:
            bad_config = {
                "connections": {
                    "default": "sqlite://:memory:",
                    "bad_alias": "not-a-valid-scheme://nope",
                },
                "apps": {"models": {"models": ["tests.testmodels"], "default_connection": "default"}},
            }
            with pytest.raises(ConfigurationError, match="Unknown DB scheme"):
                await ctx.init(config=bad_config)

            good_config = {
                "connections": {"default": "sqlite://:memory:"},
                "apps": {"models": {"models": ["tests.testmodels"], "default_connection": "default"}},
            }
            await ctx.init(config=good_config)

            assert list(ctx.connections.db_config.keys()) == ["default"]

    @pytest.mark.asyncio
    async def test_reinit_with_nonexistent_module_does_not_tear_down_working_context(self):
        """A working context must survive a rejected re-init attempt - the failing call's
        argument validation (here: a module that doesn't exist) must run before any of the
        original, still-working connections/apps are torn down."""
        from tests.testmodels import Tournament

        async with HareContext() as ctx:
            await ctx.init(HareConfig.from_db_url("sqlite://:memory:", {"models": ["tests.testmodels"]}))
            await ctx.generate_schemas()
            tournament = await Tournament.objects.create(name="Original")

            with pytest.raises(ConfigurationError, match='Module "tests.does_not_exist" not found'):
                await ctx.init(HareConfig.from_db_url("sqlite://:memory:", {"models": ["tests.does_not_exist"]}))

            assert ctx.inited is True
            assert await Tournament.objects.get(id=tournament.id) == tournament

    @pytest.mark.asyncio
    async def test_hare_reinit_with_an_invalid_config_does_not_tear_down_working_context(self):
        """Same as above, but exercised through Hare.init() (which re-inits whatever context is
        currently active) and the rejected call's problem is a configuration without connections
        rather than a bad module."""
        from hare import Hare
        from tests.testmodels import Tournament

        async with HareContext() as ctx:
            await ctx.init(HareConfig.from_db_url("sqlite://:memory:", {"models": ["tests.testmodels"]}))
            await ctx.generate_schemas()
            tournament = await Tournament.objects.create(name="Original")

            with pytest.raises(ConfigurationError, match="connections"):
                await Hare.init({"connections": {}, "apps": {}})

            assert ctx.inited is True
            assert await Tournament.objects.get(id=tournament.id) == tournament


class TestGenerateSchemas:
    """Test cases for generate_schemas method."""

    @pytest.mark.asyncio
    async def test_generate_schemas_raises_when_not_initialized(self):
        """generate_schemas() raises when context not initialized."""
        ctx = HareContext()

        with pytest.raises(ConfigurationError) as exc_info:
            await ctx.generate_schemas()

        assert "Context not initialized" in str(exc_info.value)


class TestHareTestContext:
    """Test cases for hare_test_context helper."""

    @pytest.mark.asyncio
    async def test_hare_test_context_creates_isolated_context(self):
        """hare_test_context creates isolated context."""
        original_ctx = HareContext.get_current()

        async with hare_test_context(["tests.testmodels"]) as ctx:
            # Context should be active
            assert HareContext.get_current() is ctx
            # Context should be initialized
            assert ctx.inited is True
            # Context should have apps
            assert ctx.apps is not None

        # Context should be restored to original (session context if present)
        assert HareContext.get_current() is original_ctx

    @pytest.mark.asyncio
    async def test_hare_test_context_multiple_isolated(self):
        """Multiple hare_test_context calls are isolated."""
        async with hare_test_context(["tests.testmodels"]) as ctx1:
            conn1 = ctx1.connections

        async with hare_test_context(["tests.testmodels"]) as ctx2:
            conn2 = ctx2.connections

        # Different context instances
        assert ctx1 is not ctx2
        # Different connection handlers
        assert conn1 is not conn2

    @pytest.mark.asyncio
    async def test_nested_context_with_different_dialect_does_not_corrupt_outer_models(self):
        """MetaInfo.basequery/basequery_all_fields/basetable are plain cached attributes set once
        by Apps._build_initial_querysets() - unlike MetaInfo.db (a property, always re-resolved
        fresh), they don't automatically follow which HareContext is "current". A nested
        hare_test_context() re-registering the SAME already-imported module (exactly what a
        dialect-specific fixture like Transactions.autonomous()'s file_db does, reusing
        tests.testmodels) used to leave the outer context's models permanently bound to the
        inner one's dialect/connections after the inner one exited - confirmed by reproducing it
        directly before this test was written. __aexit__ now re-runs the restored context's own
        binding pass to reclaim its models from that leak."""
        from tests.testmodels import IntFields

        async with hare_test_context(["tests.testmodels"], connection_label="models") as outer_ctx:
            outer_dialect = IntFields._meta.basequery.QUERY_CLS

            async with hare_test_context(
                ["tests.testmodels"], db_url="sqlite://:memory:", connection_label="models"
            ) as inner_ctx:
                assert HareContext.get_current() is inner_ctx
                # the inner context re-registered the same module - same class object, mutated
                assert (
                    IntFields._meta.basequery.QUERY_CLS
                    is inner_ctx.get_model("models", "IntFields")._meta.basequery.QUERY_CLS
                )

            # back to the outer context - its own dialect must be reclaimed, not left as the
            # inner context's
            assert HareContext.get_current() is outer_ctx
            assert IntFields._meta.basequery.QUERY_CLS is outer_dialect
            # and the outer context's models still actually work end to end, not just look right
            await IntFields.objects.create(intnum=1)
            assert await IntFields.objects.filter(intnum=1).exists()

    @pytest.mark.asyncio
    async def test_sql_rendered_with_another_contexts_query_builder_is_not_reused_afterwards(self):
        """The outer context's models used (e.g. from a sibling task) while a nested context has
        bound them to another dialect's query builder render that builder's SQL - a known
        limitation for that window. Once they are bound back, none of that SQL may still be served
        from the outer context's own caches: it used to stay cached under the outer dialect/
        connection key forever."""
        from hare.dialects.postgresql.query import PostgresqlQuery
        from tests.testmodels import IntFields

        async with hare_test_context(["tests.testmodels"], connection_label="models"):
            created = await IntFields.objects.create(intnum=1)
            meta = IntFields._meta
            own_basequery, own_basequery_all_fields = meta.basequery, meta.basequery_all_fields
            Caches.forget_model_caches([IntFields])
            foreign_basequery = PostgresqlQuery.from_(meta.basetable)
            meta.basequery = foreign_basequery
            meta.basequery_all_fields = foreign_basequery.select(*meta.db_fields)
            try:
                for query_factory in (
                    lambda: IntFields.objects.get(intnum=1),
                    lambda: IntFields.objects.get(pk=created.pk),
                    lambda: IntFields.objects.filter(intnum=1).count(),
                    lambda: IntFields.objects.filter(intnum__gte=1).values_list("intnum", flat=True),
                    lambda: created.save(update_fields=["intnum"]),
                ):
                    with contextlib.suppress(Exception):
                        await query_factory()
            finally:
                meta.basequery, meta.basequery_all_fields = own_basequery, own_basequery_all_fields

            assert (await IntFields.objects.get(intnum=1)).pk == created.pk
            assert (await IntFields.objects.get(pk=created.pk)).intnum == 1
            assert await IntFields.objects.filter(intnum=1).count() == 1
            assert await IntFields.objects.filter(intnum__gte=1).values_list("intnum", flat=True) == [1]
            created.intnum = 2
            await created.save(update_fields=["intnum"])
            await created.delete()
            assert not await IntFields.objects.exists()

    @pytest.mark.asyncio
    async def test_reregistering_same_module_under_different_app_label_raises_clear_error(self):
        """Model._meta.app is a plain attribute on the model CLASS, not scoped to any one
        HareContext - re-importing the same already-imported module (Python's own module
        cache) in a later, unrelated context still returns the SAME classes, still carrying
        whatever app label they were first registered under. A sequential context registering
        it again under a DIFFERENT app label must fail loudly and specifically, not silently
        leave the new app's registry empty with only a generic "has no models" warning."""
        import sys
        import types

        from hare import fields
        from hare.models import Model

        module_name = "tests._relabel_conflict_models"

        class RelabelConflictModel(Model):
            id = fields.IntField(primary_key=True)

        module = types.ModuleType(module_name)
        setattr(module, "RelabelConflictModel", RelabelConflictModel)  # noqa: B010
        sys.modules[module_name] = module

        try:
            async with hare_test_context([module_name], app_label="appA") as ctx1:
                assert ctx1.get_model("appA", "RelabelConflictModel") is RelabelConflictModel

            with pytest.raises(
                ConfigurationError,
                match=f'Module "{module_name}" has no models for app label "appB"',
            ):
                async with hare_test_context([module_name], app_label="appB"):
                    pass
        finally:
            sys.modules.pop(module_name, None)


class TestInitIntegration:
    """Integration test cases for init method."""

    @pytest.mark.asyncio
    async def test_init_with_db_url_and_modules(self):
        """init() with db_url and modules initializes context correctly."""
        async with HareContext() as ctx:
            await ctx.init(HareConfig.from_db_url("sqlite://:memory:", {"models": ["tests.testmodels"]}))

            # Context should be initialized
            assert ctx.inited is True

            # Connections should be populated
            assert ctx._connections is not None
            # Should be able to get the default connection
            conn = ctx.connections.get("default")
            assert conn is not None

            # Apps should be populated
            assert ctx.apps is not None
            # Should be able to get a model
            Author = ctx.get_model("models", "Author")
            assert Author.__name__ == "Author"

    @pytest.mark.asyncio
    async def test_generate_schemas_creates_tables(self):
        """generate_schemas() creates tables in the database."""
        async with HareContext() as ctx:
            await ctx.init(HareConfig.from_db_url("sqlite://:memory:", {"models": ["tests.testmodels"]}))
            await ctx.generate_schemas()

            # Verify tables exist by querying sqlite_master
            conn = ctx.connections.get("default")
            result = await conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='author'")
            tables = [row["name"] for row in result[1]]
            assert "author" in tables

    @pytest.mark.asyncio
    async def test_full_context_lifecycle_with_crud(self):
        """Full context lifecycle with model CRUD operations."""
        original_ctx = HareContext.get_current()

        async with HareContext() as ctx:
            await ctx.init(HareConfig.from_db_url("sqlite://:memory:", {"models": ["tests.testmodels"]}))
            await ctx.generate_schemas()

            # Import model inside test to ensure it uses active context
            from tests.testmodels import Author

            # CREATE
            author = await Author.objects.create(name="Test Author")
            assert author.id is not None
            assert author.name == "Test Author"

            # READ
            fetched = await Author.objects.get(id=author.id)
            assert fetched.name == "Test Author"

            # UPDATE
            fetched.name = "Updated Author"
            await fetched.save()
            updated = await Author.objects.get(id=author.id)
            assert updated.name == "Updated Author"

            # DELETE
            await updated.delete()
            count = await Author.objects.filter(id=author.id).count()
            assert count == 0

        # Context should be restored to original (session context if present)
        assert HareContext.get_current() is original_ctx


class TestFailedReinit:
    """A re-init that fails leaves the context and its models on the previous configuration."""

    class RouterWithArgument:
        def __init__(self, alias):
            self.alias = alias

    class PreviousRouter:
        def db_for_read(self, model):
            return None

    async def _init_with_row(self, ctx):
        await ctx.init(
            config={
                "connections": {"default": "sqlite://:memory:"},
                "apps": {"models": {"models": ["tests.testmodels"], "default_connection": "default"}},
            },
            routers=[TestFailedReinit.PreviousRouter],
        )
        await ctx.generate_schemas()
        from tests.testmodels import Author

        await Author.objects.create(name="before")
        return Author

    def _reinit_config(self, models_module="tests.testmodels"):
        return {
            "connections": {"other": "sqlite://:memory:"},
            "apps": {"models": {"models": [models_module], "default_connection": "other"}},
        }

    async def _assert_previous_configuration(self, ctx, Author):
        assert ctx.connections.db_config.keys() == {"default"}
        assert Author._meta.default_connection == "default"
        assert ctx.routers == [TestFailedReinit.PreviousRouter]
        assert [author.name for author in await Author.objects.all()] == ["before"]

    @pytest.mark.asyncio
    async def test_unknown_router_path(self):
        """A router path that can't be imported fails before anything is replaced."""
        async with HareContext() as ctx:
            Author = await self._init_with_row(ctx)
            with pytest.raises(ConfigurationError, match="Can't import router"):
                await ctx.init(config=self._reinit_config(), routers=["no.such.Router"])
            await self._assert_previous_configuration(ctx, Author)

    @pytest.mark.asyncio
    async def test_router_that_cannot_be_instantiated(self):
        """A router class needing constructor arguments fails after the new apps were built -
        the models are bound back to the previous configuration."""
        async with HareContext() as ctx:
            Author = await self._init_with_row(ctx)
            with pytest.raises(ConfigurationError, match="Can't instantiate router"):
                await ctx.init(config=self._reinit_config(), routers=[TestFailedReinit.RouterWithArgument])
            await self._assert_previous_configuration(ctx, Author)

    @pytest.mark.asyncio
    async def test_unknown_models_module(self):
        """A models module that can't be imported keeps the previous configuration."""
        async with HareContext() as ctx:
            Author = await self._init_with_row(ctx)
            with pytest.raises(ConfigurationError):
                await ctx.init(
                    config=self._reinit_config("tests.no_such_models_module"),
                    routers=[TestFailedReinit.PreviousRouter],
                )
            await self._assert_previous_configuration(ctx, Author)

    @pytest.mark.asyncio
    async def test_global_fallback_held_by_another_context(self):
        """_enable_global_fallback=True while another context is the global one fails before
        anything is replaced."""
        other = HareContext()
        HareContext.set_global(other)
        async with HareContext() as ctx:
            Author = await self._init_with_row(ctx)
            with pytest.raises(ConfigurationError, match="Global context fallback is already enabled"):
                await ctx.init(
                    config=self._reinit_config(),
                    routers=[TestFailedReinit.PreviousRouter],
                    _enable_global_fallback=True,
                )
            await self._assert_previous_configuration(ctx, Author)
        assert HareContext.global_context is other


class TestNestedContextRebinding:
    """Leaving a nested context that loaded the same models with another configuration binds them
    back to the outer context."""

    @pytest.mark.asyncio
    async def test_models_return_to_outer_connection_and_table_names(self):
        from tests.testmodels import Author

        async with HareContext() as outer:
            await outer.init(
                config={
                    "connections": {"default": "sqlite://:memory:"},
                    "apps": {"models": {"models": ["tests.testmodels"], "default_connection": "default"}},
                }
            )
            await outer.generate_schemas()
            await Author.objects.create(name="outer")
            async with HareContext() as inner:
                await inner.init(
                    config={
                        "connections": {"other": "sqlite://:memory:"},
                        "apps": {"models": {"models": ["tests.testmodels"], "default_connection": "other"}},
                    },
                    table_name_generator=lambda model: f"inner_{model.__name__.lower()}",
                )
                await inner.generate_schemas()
                await Author.objects.create(name="inner")
                assert Author._meta.db_table == "inner_author"

            assert Author._meta.default_connection == "default"
            assert Author._meta.db_table == "author"
            await Author.objects.create(name="outer again")
            assert sorted(author.name for author in await Author.objects.all()) == ["outer", "outer again"]


class TestModelContextResolution:
    """Test cases for model context resolution."""

    @pytest.mark.asyncio
    async def test_model_uses_context_connections_when_active(self):
        """Model uses context when context is active."""
        async with HareContext() as ctx:
            await ctx.init(HareConfig.from_db_url("sqlite://:memory:", {"models": ["tests.testmodels"]}))
            await ctx.generate_schemas()

            from tests.testmodels import Author

            # Create a record
            author = await Author.objects.create(name="Context Author")
            assert author.id is not None

            # Verify we can query it
            all_authors = await Author.objects.all()
            assert len(all_authors) == 1
            assert all_authors[0].name == "Context Author"

    @pytest.mark.asyncio
    async def test_sequential_contexts_isolated(self):
        """Sequential contexts are isolated from each other."""
        from tests.testmodels import Author

        # First context creates its own author
        async with HareContext() as ctx1:
            await ctx1.init(HareConfig.from_db_url("sqlite://:memory:", {"models": ["tests.testmodels"]}))
            await ctx1.generate_schemas()

            await Author.objects.create(name="Author in Context 1")
            authors_in_ctx1 = await Author.objects.all()
            assert len(authors_in_ctx1) == 1
            assert authors_in_ctx1[0].name == "Author in Context 1"

        # Second context should start with empty database
        async with HareContext() as ctx2:
            await ctx2.init(HareConfig.from_db_url("sqlite://:memory:", {"models": ["tests.testmodels"]}))
            await ctx2.generate_schemas()

            # Should be empty - isolated from first context
            authors_before = await Author.objects.all()
            assert len(authors_before) == 0, "Second context should start empty"

            await Author.objects.create(name="Author in Context 2")
            authors_in_ctx2 = await Author.objects.all()
            assert len(authors_in_ctx2) == 1
            assert authors_in_ctx2[0].name == "Author in Context 2"


class TestTimezoneAndRouters:
    """Test cases for timezone and routers configuration."""

    class TestRouter:
        pass

    def test_context_default_timezone_settings(self):
        """Context has default timezone settings."""
        ctx = HareContext()
        assert ctx.use_tz is True
        assert ctx.timezone == "UTC"
        assert ctx.routers == []

    @pytest.mark.asyncio
    async def test_init_with_timezone_settings(self):
        """Context can be initialized with timezone settings."""
        async with HareContext() as ctx:
            await ctx.init(
                HareConfig.from_db_url("sqlite://:memory:", {"models": ["tests.testmodels"]}),
                use_tz=True,
                timezone="America/New_York",
            )

            assert ctx.use_tz is True
            assert ctx.timezone == "America/New_York"

    @pytest.mark.asyncio
    async def test_init_with_config_dict_timezone(self):
        """Timezone settings from config dict are used."""
        async with HareContext() as ctx:
            await ctx.init(
                config={
                    "connections": {"default": "sqlite://:memory:"},
                    "apps": {"models": {"models": ["tests.testmodels"]}},
                    "use_tz": True,
                    "timezone": "Europe/London",
                }
            )

            assert ctx.use_tz is True
            assert ctx.timezone == "Europe/London"

    @pytest.mark.asyncio
    async def test_hare_test_context_with_timezone(self):
        """hare_test_context supports timezone parameters."""
        async with hare_test_context(
            ["tests.testmodels"],
            use_tz=True,
            timezone="Asia/Tokyo",
        ) as ctx:
            assert ctx.use_tz is True
            assert ctx.timezone == "Asia/Tokyo"

    @pytest.mark.asyncio
    async def test_outer_context_timezone_restored_after_inner_context_exits(self):
        """Timezone.name()/get_use_tz() must resolve to whichever HareContext is currently
        active - reading the contextvar directly means exiting the inner context's `async with`
        naturally restores the outer context's own settings, with no separate re-sync step
        needed (unlike the old os.environ-based design, where nothing re-synced timezone
        resolution on the way back out and a nested context's settings could leak)."""
        async with HareContext() as outer:
            await outer.init(
                HareConfig.from_db_url("sqlite://:memory:", {"models": ["tests.testmodels"]}),
                use_tz=True,
                timezone="America/New_York",
            )
            assert Timezone.name() == "America/New_York"
            assert Timezone.get_use_tz() is True

            async with HareContext() as inner:
                await inner.init(
                    HareConfig.from_db_url("sqlite://:memory:", {"models": ["tests.testmodels"]}),
                    use_tz=False,
                    timezone="Europe/Moscow",
                )
                assert Timezone.name() == "Europe/Moscow"
                assert Timezone.get_use_tz() is False

            assert Timezone.name() == "America/New_York"
            assert Timezone.get_use_tz() is True

    @pytest.mark.asyncio
    async def test_naive_datetime_is_interpreted_consistently_across_create_and_filter(self):
        """A naive datetime given to Model(**kwargs) construction (routed through
        to_python) and one given to .filter() (routed straight through
        to_db_value) must represent the SAME instant - otherwise .filter(field=naive) can
        silently miss the very row .create() with the identical naive value just inserted, since
        the two paths disagreed on which zone a naive value is assumed to be in."""
        import datetime

        from tests.testmodels import DatetimeFields

        async with hare_test_context(
            ["tests.testmodels"],
            use_tz=True,
            timezone="America/New_York",
        ):
            naive = datetime.datetime(2020, 1, 1, 10, 0, 0)
            created = await DatetimeFields.objects.create(datetime=naive)

            found = await DatetimeFields.objects.filter(datetime=naive).first()
            assert found is not None
            assert found.pk == created.pk

    @pytest.mark.asyncio
    async def test_naive_datetime_assigned_via_construction_and_via_direct_mutation_agree(self):
        """Model(**kwargs) construction and a direct attribute mutation followed by .save() -
        the two places a naive datetime can reach to_db_value from - must persist the same
        instant for the same naive input."""
        import datetime

        from tests.testmodels import DatetimeFields

        async with hare_test_context(
            ["tests.testmodels"],
            use_tz=True,
            timezone="America/New_York",
        ):
            naive = datetime.datetime(2020, 1, 1, 10, 0, 0)

            via_kwargs = await DatetimeFields.objects.create(datetime=naive)

            via_mutation = await DatetimeFields.objects.create(datetime=datetime.datetime(2000, 1, 1))
            object.__setattr__(via_mutation, "datetime", naive)
            await via_mutation.save()

            reread_kwargs = await DatetimeFields.objects.get(pk=via_kwargs.pk)
            reread_mutation = await DatetimeFields.objects.get(pk=via_mutation.pk)
            assert reread_kwargs.datetime == reread_mutation.datetime

    @pytest.mark.asyncio
    async def test_init_routers_empty_list(self):
        """_init_routers with empty list initializes correctly."""
        async with HareContext() as ctx:
            await ctx.init(HareConfig.from_db_url("sqlite://:memory:", {"models": ["tests.testmodels"]}), routers=[])

            assert ctx.routers == []

    @pytest.mark.asyncio
    async def test_init_routers_with_type(self):
        """_init_routers accepts router type directly."""

        async with HareContext() as ctx:
            await ctx.init(
                HareConfig.from_db_url("sqlite://:memory:", {"models": ["tests.testmodels"]}),
                routers=[TestTimezoneAndRouters.TestRouter],
            )

            assert ctx.routers == [TestTimezoneAndRouters.TestRouter]

    @pytest.mark.asyncio
    async def test_init_routers_with_string_path(self):
        """_init_routers accepts router as string path."""
        async with HareContext() as ctx:
            await ctx.init(
                HareConfig.from_db_url("sqlite://:memory:", {"models": ["tests.testmodels"]}),
                routers=["hare.core.router.ConnectionRouter"],
            )

            from hare.core.router import ConnectionRouter

            assert ctx.routers == [ConnectionRouter]

    @pytest.mark.asyncio
    async def test_init_routers_mixed_types_and_strings(self):
        """_init_routers accepts mixed router types and strings."""

        async with HareContext() as ctx:
            await ctx.init(
                HareConfig.from_db_url("sqlite://:memory:", {"models": ["tests.testmodels"]}),
                routers=[TestTimezoneAndRouters.TestRouter, "hare.core.router.ConnectionRouter"],
            )

            from hare.core.router import ConnectionRouter

            assert ctx.routers == [TestTimezoneAndRouters.TestRouter, ConnectionRouter]

    @pytest.mark.asyncio
    async def test_init_routers_invalid_router_type(self):
        """_init_routers raises on invalid router type."""
        async with HareContext() as ctx:
            with pytest.raises(ConfigurationError) as exc_info:
                await ctx.init(
                    HareConfig.from_db_url("sqlite://:memory:", {"models": ["tests.testmodels"]}),
                    routers=["not_a_valid_router_path"],
                )

            assert "Can't import router" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_init_routers_invalid_router_item(self):
        """_init_routers raises when router is neither string nor type."""
        async with HareContext() as ctx:
            with pytest.raises(ConfigurationError) as exc_info:
                await ctx.init(
                    HareConfig.from_db_url("sqlite://:memory:", {"models": ["tests.testmodels"]}), routers=[123]
                )

            assert "Router must be either str or type" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_init_routers_none_uses_empty_list(self):
        """_init_routers with None uses empty list."""
        async with HareContext() as ctx:
            await ctx.init(HareConfig.from_db_url("sqlite://:memory:", {"models": ["tests.testmodels"]}), routers=None)

            assert ctx.routers == []

    @pytest.mark.asyncio
    async def test_router_dispatch_is_isolated_per_context(self):
        """The actual db_for_read/db_for_write dispatch object must be per-HareContext, not a
        shared process-wide singleton - two contexts with different `routers=` configs (or one
        context's .init() running after another's) must never affect each other's routing."""
        async with HareContext() as ctx_a:
            await ctx_a.init(
                HareConfig.from_db_url("sqlite://:memory:", {"models": ["tests.testmodels"]}),
                routers=[TestTimezoneAndRouters.TestRouter],
            )
            assert [type(r) for r in ctx_a.router._routers] == [TestTimezoneAndRouters.TestRouter]

            async with HareContext() as ctx_b:
                await ctx_b.init(HareConfig.from_db_url("sqlite://:memory:", {"models": ["tests.testmodels"]}))
                assert ctx_b.router._routers == []
                assert ctx_a.router is not ctx_b.router
                # ctx_b's routerless init must not have reset ctx_a's dispatch.
                assert [type(r) for r in ctx_a.router._routers] == [TestTimezoneAndRouters.TestRouter]

            # Restoring ctx_a as current afterward must still see its own router state.
            assert [type(r) for r in ctx_a.router._routers] == [TestTimezoneAndRouters.TestRouter]


class TestContextObservers:
    """``HareContext.observe()`` - an observer getting the events while its context is the current
    one, dropped when the context is left, unlike a process observer (``Observers.observe()``)."""

    @pytest.fixture(autouse=True)
    def _clear_process_observers(self):
        Observers.process_observers.clear()
        yield
        Observers.process_observers.clear()

    @staticmethod
    def notify(event_type):
        if event_type is QueryExecuted:
            Observers.notify(QueryExecuted("SELECT 1", None, 1.0, None, "default"))
        else:
            Observers.notify(TransactionEvent(TransactionEventType.BEGIN, "default", 0.0, None))

    @pytest.mark.parametrize("event_type", [QueryExecuted, TransactionEvent])
    @pytest.mark.asyncio
    async def test_observer_gets_events_while_its_context_is_current(self, event_type):
        calls = []

        async with HareContext() as ctx:
            assert ctx.observe(event_type, calls.append) == calls.append
            self.notify(event_type)
            assert len(calls) == 1

        assert not ctx.observers.observes(event_type)
        async with HareContext():
            self.notify(event_type)
        self.notify(event_type)
        assert len(calls) == 1

    @pytest.mark.parametrize("event_type", [QueryExecuted, TransactionEvent])
    @pytest.mark.asyncio
    async def test_process_observer_survives_context_exit(self, event_type):
        calls = []
        Observers.observe(event_type, calls.append)

        async with HareContext():
            pass

        self.notify(event_type)
        assert len(calls) == 1

    @pytest.mark.parametrize("event_type", [QueryExecuted, TransactionEvent])
    @pytest.mark.asyncio
    async def test_outer_context_observer_pauses_inside_an_inner_context(self, event_type):
        calls = []

        async with HareContext() as outer:
            outer.observe(event_type, calls.append)
            async with HareContext():
                self.notify(event_type)
            assert calls == []
            self.notify(event_type)
            assert len(calls) == 1

        assert not outer.observers.observes(event_type)

    @pytest.mark.asyncio
    async def test_leaving_a_context_leaves_no_observer_counted(self):
        """The count of observers decides whether a query builds its event at all - an observer
        of a context that was left must not keep it from being skipped."""
        before = ObserverSet.total_count
        async with HareContext() as ctx:
            ctx.observe(QueryExecuted, lambda event: None)
            ctx.observe(TransactionEvent, lambda event: None)
            assert ObserverSet.total_count == before + 2
        assert ObserverSet.total_count == before


class TestHareConfigValidation:
    """Test cases for HareConfig validation in ctx.init()."""

    @pytest.mark.asyncio
    async def test_init_accepts_hare_config_object(self):
        """ctx.init() accepts HareConfig object directly."""
        from hare.core.config import AppConfig, DBUrlConfig, HareConfig

        config = HareConfig(
            connections={"default": DBUrlConfig("sqlite://:memory:")},
            apps={"models": AppConfig(models=["tests.testmodels"])},
            use_tz=True,
            timezone="UTC",
        )

        async with HareContext() as ctx:
            await ctx.init(config=config)
            assert ctx.inited is True
            assert ctx.use_tz is True

    @pytest.mark.asyncio
    async def test_init_validates_dict_config(self):
        """ctx.init() validates dict config and raises ConfigurationError on issues."""
        async with HareContext() as ctx:
            # Config with missing 'models' in app should raise ConfigurationError
            # because HareConfig.from_dict validates the structure
            with pytest.raises(ConfigurationError) as exc_info:
                await ctx.init(
                    config={
                        "connections": {"default": "sqlite://:memory:"},
                        "apps": {"models": {}},  # Missing 'models' key
                    }
                )
            assert "models" in str(exc_info.value).lower()
