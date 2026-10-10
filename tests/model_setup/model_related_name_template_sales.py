"""
Testing Models for related_name %(app_label)s/%(class)s templating - "sales" app side.
"""

from tests.model_setup.model_related_name_template_base import TaggedItem


class Item(TaggedItem):
    class Meta:
        table = "sales_item"
