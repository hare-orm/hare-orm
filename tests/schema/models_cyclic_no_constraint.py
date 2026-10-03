"""
This is the testing Models - cyclic FK references where no real DDL-level dependency exists
because every FK involved in the cycle is db_constraint=False, plus a mixed case where only
one edge of the cycle is unconstrained and the other must still be tracked as a real dependency.
"""

from hare import fields
from hare.models import Model


class One(Model):
    tournament: fields.ForeignKeyRelation["Two"] = fields.ForeignKeyField(
        "models.Two", related_name="events", db_constraint=False
    )


class Two(Model):
    tournament: fields.ForeignKeyRelation["Three"] = fields.ForeignKeyField(
        "models.Three", related_name="events", db_constraint=False
    )


class Three(Model):
    tournament: fields.ForeignKeyRelation["One"] = fields.ForeignKeyField(
        "models.One", related_name="events", db_constraint=False
    )


class SymmetricA(Model):
    other: fields.ForeignKeyNullableRelation["SymmetricB"] = fields.ForeignKeyField(
        "models.SymmetricB", db_constraint=False, related_name="symmetric_a_set", null=True
    )


class SymmetricB(Model):
    other: fields.ForeignKeyNullableRelation[SymmetricA] = fields.ForeignKeyField(
        "models.SymmetricA", db_constraint=False, related_name="symmetric_b_set", null=True
    )


class AsymmetricC(Model):
    other: fields.ForeignKeyNullableRelation["AsymmetricD"] = fields.ForeignKeyField(
        "models.AsymmetricD", related_name="asymmetric_c_set", null=True
    )


class AsymmetricD(Model):
    other: fields.ForeignKeyNullableRelation[AsymmetricC] = fields.ForeignKeyField(
        "models.AsymmetricC", db_constraint=False, related_name="asymmetric_d_set", null=True
    )
