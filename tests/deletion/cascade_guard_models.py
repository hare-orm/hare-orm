"""Models for the guard checks of a cascade hare runs itself (db_constraint=False relations)."""

from hare import fields
from hare.models import Model


class LooseGuardedNode(Model):
    """A tree deleted by hare's own CASCADE, a node guarding another by PROTECT."""

    name = fields.CharField(max_length=50)
    parent = fields.ForeignKeyField(
        "models.LooseGuardedNode", related_name="children", null=True, on_delete=fields.CASCADE, db_constraint=False
    )
    guardian = fields.ForeignKeyField(
        "models.LooseGuardedNode", related_name="guarded", null=True, on_delete=fields.PROTECT, db_constraint=False
    )


class LooseRestrictedNode(Model):
    """A tree deleted by hare's own CASCADE, a node restricting another by RESTRICT."""

    name = fields.CharField(max_length=50)
    parent = fields.ForeignKeyField(
        "models.LooseRestrictedNode", related_name="children", null=True, on_delete=fields.CASCADE, db_constraint=False
    )
    blocker = fields.ForeignKeyField(
        "models.LooseRestrictedNode", related_name="blocked", null=True, on_delete=fields.RESTRICT, db_constraint=False
    )


class GuardedAccount(Model):
    """A row a row of its own cascade guards by a database-enforced PROTECT."""

    name = fields.CharField(max_length=50)


class AccountLedger(Model):
    account = fields.ForeignKeyField("models.GuardedAccount", related_name="ledgers", on_delete=fields.CASCADE)


class LedgerEntry(Model):
    """Removed with its ledger - and guarding the account the ledger is removed with."""

    ledger = fields.ForeignKeyField("models.AccountLedger", related_name="entries", on_delete=fields.CASCADE)
    account = fields.ForeignKeyField("models.GuardedAccount", related_name="entries", on_delete=fields.PROTECT)
