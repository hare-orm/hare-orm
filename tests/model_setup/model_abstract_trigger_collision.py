"""Shared abstract mixin for a Meta.triggers name-collision guard check - two concrete siblings
inherit the SAME explicitly-named Trigger unchanged from one abstract base."""

from hare import fields
from hare.ddl.enums import TriggerEvent
from hare.ddl.triggers import Trigger
from hare.models import Model


class AbstractTriggeredBase(Model):
    name = fields.CharField(50)

    class Meta:
        abstract = True
        triggers = (Trigger(name="trg_shared_touch", on=TriggerEvent.INSERT, body="SELECT 1;"),)


class TriggeredSiblingM(AbstractTriggeredBase):
    pass


class TriggeredSiblingN(AbstractTriggeredBase):
    pass
