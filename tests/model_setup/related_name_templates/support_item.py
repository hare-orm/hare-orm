"""
Testing Models for related_name %(app_label)s/%(class)s templating - "support" app side.
"""

from tests.model_setup.related_name_templates.tagged_item_base import TaggedItem


class Item(TaggedItem):
    class Meta:
        table = "support_item"
