"""
Testing Models for related_name %(app_label)s/%(class)s templating - "sales" app side.
"""

from tests.model_setup.related_name_templates.tagged_item_base import TaggedItem


class Item(TaggedItem):
    class Meta:
        table = "sales_item"
