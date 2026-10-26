from hare import fields
from hare.models import Model


class ContractAnchor(Model):
    """The one model of the contract tests' databases - hare opens a context on models; the tables the tests
    need, their migrations create."""

    id = fields.IntField(primary_key=True, generated=False)
