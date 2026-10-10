"""Companion models for tests/model_setup/test_abstract_model_inheritance.py.

ForeignKeyField's model_name= accepts either a string "app.Model" or a model CLASS OBJECT
directly (see ForeignKeyFieldInstance.validate_model_name). The string form is safe against
referencing an abstract model - Apps._discover_models() never registers abstract models under any
app label, so "models.AbstractAttempt" raises a clear ConfigurationError. This module targets the
class-object form instead.
"""

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
