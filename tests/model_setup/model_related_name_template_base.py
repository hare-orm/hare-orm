"""
Testing Models for related_name %(app_label)s/%(class)s templating - an abstract base
declaring the FK once, so each concrete subclass (registered in a different app) gets its
own template substitution instead of colliding on the same literal backward-accessor name.

Never listed directly in any app's "models" - only imported by the per-app concrete
subclass modules, so it's never itself scanned by app discovery.
"""

from hare import fields
from hare.models import Model
from tests.model_setup.model_related_name_template_tag import Tag


class TaggedItem(Model):
    tag: fields.ForeignKeyRelation[Tag] = fields.ForeignKeyField("tags.Tag", related_name="%(app_label)s_items")

    class Meta:
        abstract = True
