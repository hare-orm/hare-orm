import pytest

from hare import Hare
from hare.core.hare_context import HareContext
from hare.exceptions import ConfigurationError
from tests.model_setup.model_related_name_template_sales import Item as SalesItem
from tests.model_setup.model_related_name_template_support import Item as SupportItem
from tests.model_setup.model_related_name_template_tag import Tag
from tests.utils.context_apps_reset import ContextAppsReset

# Save the original classproperty before any test can shadow it
_original_apps_prop = Hare.__dict__["apps"]


async def _reset_hare():
    """Helper to reset Hare state before each test.

    Note: We MUST NOT set Hare.apps = None
    because it is a classproperty and setting it shadows the property
    with a class attribute, breaking future access.
    """
    if not isinstance(Hare.__dict__.get("apps"), type(_original_apps_prop)):
        type.__setattr__(Hare, "apps", _original_apps_prop)

    ctx = HareContext.get_current()
    if ctx is not None:
        if ctx._connections is not None:
            ctx._connections._storage.clear()
            ctx._connections._db_config = None
            ctx._connections = None
        ctx._apps = None
        ctx._inited = False
        ctx._default_connection = None
    else:
        ctx = HareContext()
        ctx.__enter__()


async def _teardown_hare():
    # Real queries were run against a real connection here (unlike the other tests in this
    # directory, which only check for a ConfigurationError) - closing it is required or the
    # event loop hangs waiting for it to die.
    await Hare.close_connections()
    ContextAppsReset.reset_apps()


@pytest.mark.asyncio
async def test_related_name_template_resolves_per_app():
    """Two apps ("sales"/"support") share a common abstract base declaring
    related_name="%(app_label)s_items" - without templating both concrete subclasses would
    collide on the same literal backward-accessor name on Tag; with it, each gets its own,
    real, working backward accessor."""
    await _reset_hare()
    try:
        await Hare.init(
            {
                "connections": {
                    "default": {
                        "engine": "sqlite+aiosqlite",
                        "credentials": {"file_path": ":memory:"},
                    }
                },
                "apps": {
                    "tags": {
                        "models": ["tests.model_setup.model_related_name_template_tag"],
                        "default_connection": "default",
                    },
                    "sales": {
                        "models": ["tests.model_setup.model_related_name_template_sales"],
                        "default_connection": "default",
                    },
                    "support": {
                        "models": ["tests.model_setup.model_related_name_template_support"],
                        "default_connection": "default",
                    },
                },
            }
        )
        await Hare.generate_schemas(safe=False)

        tag = await Tag.objects.create(name="urgent")
        await SalesItem.objects.create(tag=tag)
        await SupportItem.objects.create(tag=tag)

        fetched_tag = await Tag.objects.all().prefetch_related("sales_items", "support_items").get(id=tag.id)
        assert len(fetched_tag.sales_items) == 1
        assert len(fetched_tag.support_items) == 1
    finally:
        await _teardown_hare()


@pytest.mark.asyncio
async def test_related_name_collision_without_template_suggests_template():
    """A plain, non-templated related_name collision must hint at %(app_label)s/%(class)s
    templating as the fix, not just say "duplicates". Uses its own dedicated model module
    (not models_dup1, which other tests also use) - a failed Hare.init() call permanently
    marks the involved model classes as `_inited`, so reusing the same module across two
    separate test functions would make the second one silently skip re-validation."""
    await _reset_hare()
    try:
        with pytest.raises(ConfigurationError, match="use a related_name template"):
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
                            "models": ["tests.model_setup.model_related_name_no_template_collision"],
                            "default_connection": "default",
                        }
                    },
                }
            )
    finally:
        await _teardown_hare()
