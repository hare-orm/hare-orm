"""Companion models for tests/model_setup/test_abstract_model_inheritance.py.

Two concrete siblings of the same abstract base, CASCADE-ing onto each other (A -> B -> A),
rather than a single self-referential model - exercises
ReverseRelationCascade.has_self_cascading_constrained_relations() against a genuine cross-model
cycle, not just the single-model self-referential case already covered by
tests/test_deep_self_referential_cascade.py."""

from hare import fields
from hare.fields.constants import CASCADE
from hare.models import Model


class AbstractCrossLinkBase(Model):
    """Contributes no fields of its own - purely a shared ancestor, so NodeA/NodeB below are
    real siblings (both go through ModelMeta's abstract-base merge/deepcopy machinery) while the
    actual CASCADE cycle is formed by their own directly-declared FK fields."""

    name = fields.CharField(50)

    class Meta:
        abstract = True


class CrossLinkNodeA(AbstractCrossLinkBase):
    linked_b: fields.ForeignKeyNullableRelation["CrossLinkNodeB"] = fields.ForeignKeyField(
        "models.CrossLinkNodeB", related_name="back_to_a", null=True, on_delete=CASCADE
    )
    back_to_a: fields.ReverseRelation["CrossLinkNodeB"]


class CrossLinkNodeB(AbstractCrossLinkBase):
    linked_a: fields.ForeignKeyNullableRelation[CrossLinkNodeA] = fields.ForeignKeyField(
        "models.CrossLinkNodeA", related_name="back_to_b", null=True, on_delete=CASCADE
    )
    back_to_b: fields.ReverseRelation[CrossLinkNodeA]
