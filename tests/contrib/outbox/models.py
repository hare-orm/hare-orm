"""Model definitions for tests/contrib/outbox/ - kept in their own module (rather than inline
per test file) since hare_test_context()/make_db_fixture() both discover models by module path.
"""

from hare import fields
from hare.contrib.outbox import OutboxEvent
from hare.ddl.constraints import UniqueConstraint


class DemoOutboxEvent(OutboxEvent):
    """The multiple-inheritance recipe collapses to single inheritance here since these tests
    have no other project base model to combine with - see docs/integrations/outbox.md for the
    ``class MyOutboxEvent(YourBaseModel, OutboxEvent)`` shape real projects use."""

    class Meta(OutboxEvent.Meta):
        pass


class TenantScopedOutboxEvent(OutboxEvent):
    """Combines OutboxEvent with a Meta.tenant_field, matching the real-world ``class
    MyOutboxEvent(YourBaseModel, OutboxEvent)`` recipe where the project's own base model
    happens to be tenant-scoped - OutboxRelay's own internal housekeeping queries (polling/
    claiming/cleanup) must still see every tenant's rows despite this, since it's framework
    machinery servicing the whole table and never runs inside an active Tenancy.scope()."""

    tenant_id = fields.IntField()

    class Meta(OutboxEvent.Meta):
        tenant_field = "tenant_id"


class SoftDeleteOutboxEvent(OutboxEvent):
    """Combines OutboxEvent with a Meta.soft_delete_field, matching the real-world ``class
    MyOutboxEvent(YourBaseModel, OutboxEvent)`` recipe where the project's own shared base model
    happens to be soft-delete-enabled - cleanup_published() must refuse this combination rather
    than silently soft-deleting (an UPDATE, not a real DELETE) instead of actually reclaiming
    storage, its whole documented purpose."""

    deleted_at = fields.DatetimeField(null=True)

    class Meta(OutboxEvent.Meta):
        soft_delete_field = "deleted_at"


class SourcedOutboxEvent(OutboxEvent):
    """A subclass with a required column of its own - publish() sets it via extra_field_values."""

    source = fields.CharField(max_length=50)

    class Meta(OutboxEvent.Meta):
        pass


class UniqueReferenceOutboxEvent(OutboxEvent):
    """A subclass with a unique column of its own - a conflict on it is not an idempotency hit."""

    external_reference = fields.CharField(max_length=50, unique=True, null=True)

    class Meta(OutboxEvent.Meta):
        pass


class TenantKeyOutboxEvent(OutboxEvent):
    """Idempotency keys unique per tenant: the key column is redeclared non-unique and covered by
    a (tenant_id, idempotency_key) unique_together instead."""

    tenant_id = fields.IntField()
    idempotency_key = fields.CharField(max_length=255, null=True)

    class Meta(OutboxEvent.Meta):
        tenant_field = "tenant_id"
        constraints = (UniqueConstraint(fields=("tenant_id", "idempotency_key")),)


class NonUniqueKeyOutboxEvent(OutboxEvent):
    """The key column redeclared without any unique constraint - keyed publishing can't work."""

    idempotency_key = fields.CharField(max_length=255, null=True)

    class Meta(OutboxEvent.Meta):
        pass
