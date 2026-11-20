"""Models for checking rejection of a foreign key declared with an abstract class as its target."""

from hare import fields
from hare.models import Model


class AbstractAttemptBase(Model):
    name = fields.CharField(50)

    class Meta:
        abstract = True


class ConcreteAttemptChild(AbstractAttemptBase):
    # References the ABSTRACT base directly by class object - a plausible (if wrong) way someone
    # might try to write a "points back at my own tree" relation without an "app.Model" string.
    parent = fields.ForeignKeyField(AbstractAttemptBase, related_name="children", null=True)
