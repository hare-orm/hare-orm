"""
Testing Models for related_name %(app_label)s/%(class)s templating - the shared FK target,
registered under its own "tags" app.
"""

from hare import fields
from hare.models import Model


class Tag(Model):
    name = fields.CharField(max_length=50)
