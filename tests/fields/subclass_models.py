from enum import Enum, IntEnum

from hare import fields
from hare.models import Model
from tests.fields.subclass_fields import EnumField, IntEnumField, MarkerDateField, XorMaskedField


class RacePlacingEnum(Enum):
    FIRST = "first"
    SECOND = "second"
    THIRD = "third"
    RUNNER_UP = "runner_up"
    DNF = "dnf"


class RaceParticipant(Model):
    id = fields.IntField(primary_key=True)
    first_name = fields.CharField(max_length=64)
    place = EnumField(RacePlacingEnum, default=RacePlacingEnum.DNF)
    predicted_place = EnumField(RacePlacingEnum, null=True)


class ContactTypeEnum(IntEnum):
    work = 1
    home = 2
    other = 3


class Contact(Model):
    id = fields.IntField(primary_key=True)
    type = IntEnumField(ContactTypeEnum, default=ContactTypeEnum.other)


class DateMarkerModel(Model):
    id = fields.IntField(primary_key=True)
    event_date = MarkerDateField()


class XorMaskedModel(Model):
    id = fields.IntField(primary_key=True)
    value = XorMaskedField()
