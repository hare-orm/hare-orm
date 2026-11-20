import pytest

from hare import Hare
from hare.core.hare_context import HareContext
from hare.exceptions import ConfigurationError
from tests.model_setup.related_name_templates.sales_item import Item as SalesItem
from tests.model_setup.related_name_templates.support_item import Item as SupportItem
from tests.model_setup.related_name_templates.tag import Tag

pytestmark = pytest.mark.sqlite_only


@pytest.mark.asyncio
async def test_related_name_template_resolves_per_app():
    """Two apps ("sales"/"support") share a common abstract base declaring
    related_name="%(app_label)s_items" - without templating both concrete subclasses would
    collide on the same literal backward-accessor name on Tag; with it, each gets its own,
    real, working backward accessor."""
    async with HareContext():
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
                        "models": ["tests.model_setup.related_name_templates.tag"],
                        "default_connection": "default",
                    },
                    "sales": {
                        "models": ["tests.model_setup.related_name_templates.sales_item"],
                        "default_connection": "default",
                    },
                    "support": {
                        "models": ["tests.model_setup.related_name_templates.support_item"],
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


@pytest.mark.database_independent
@pytest.mark.asyncio
async def test_related_name_collision_without_template_suggests_template():
    """A plain, non-templated related_name collision must hint at %(app_label)s/%(class)s
    templating as the fix, not just say "duplicates". The dedicated model module isolates
    this diagnostic from other invalid relation declarations."""
    async with HareContext():
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
                            "models": ["tests.model_setup.related_name_templates.collision_without_template"],
                            "default_connection": "default",
                        }
                    },
                }
            )
