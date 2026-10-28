"""The samples of the Meta option matrix - two values of every option an abstract base can pass to its
concrete models. An option hare takes that has no sample is in ``META_OPTIONS_NOT_SAMPLED`` with why."""

from __future__ import annotations

from hare.contrib.outbox.wakeups.in_process_wakeup import InProcessWakeup
from hare.ddl import (
    DatabaseFunction,
    DatabaseSequence,
    Dictionary,
    Grant,
    MaterializedView,
    Policy,
    PolicyCommand,
    Privilege,
    RawSQLTerm,
    RowLevelSecurity,
    Trigger,
    TriggerEvent,
    View,
)
from hare.ddl.constraints import UniqueConstraint
from hare.ddl.indexes import Index
from hare.dialects.sqlite.sqlite_table_options import SqliteTableOptions
from hare.instrumentation.enums import ChangePayload
from hare.query.managers.manager import Manager
from tests.contracts.meta_options.meta_option_sample import MetaOptionSample

OUTBOX_LABEL = "contracts.MetaOutboxEvent"


def make_change_capture(payload: ChangePayload):
    """A change capture into an outbox named by its label."""
    from hare.contrib.outbox import ChangeCapture

    return ChangeCapture(OUTBOX_LABEL, payload=payload)


META_OPTION_SAMPLES = (
    MetaOptionSample("app", lambda: "first_app", lambda: "second_app"),
    MetaOptionSample("table", lambda: "base_table", lambda: "override_table"),
    MetaOptionSample("managed", lambda: False, lambda: True),
    MetaOptionSample("schema", lambda: "first_schema", lambda: "second_schema"),
    MetaOptionSample("ordering", lambda: ("name",), lambda: ("-name",), cleared_value=()),
    MetaOptionSample("get_latest_by", lambda: "name", lambda: "id", cleared_value=()),
    MetaOptionSample("soft_delete_field", lambda: "deleted_at", lambda: "removed_at"),
    MetaOptionSample("soft_delete_hard_cascade", lambda: True, lambda: False, {"soft_delete_field": "deleted_at"}),
    MetaOptionSample("optimistic_lock_field", lambda: "version", lambda: "revision"),
    MetaOptionSample("tenant_field", lambda: "company_id", lambda: "owner_id"),
    MetaOptionSample("track_dirty_fields", lambda: True, lambda: False),
    MetaOptionSample("returning", lambda: True, lambda: False),
    MetaOptionSample("table_description", lambda: "the base table", lambda: "the table of the model"),
    MetaOptionSample(
        "change_capture",
        lambda: make_change_capture(ChangePayload.AFTER),
        lambda: make_change_capture(ChangePayload.KEYS),
    ),
    MetaOptionSample(
        "indexes",
        lambda: [Index(fields=["name"], name="ix_meta_base")],
        lambda: [Index(fields=["name"], name="ix_meta_override")],
    ),
    MetaOptionSample(
        "constraints",
        lambda: (UniqueConstraint(fields=["name"], name="uq_meta_base"),),
        lambda: (UniqueConstraint(fields=["name"], name="uq_meta_override"),),
        cleared_value=(),
    ),
    MetaOptionSample("primary_key", lambda: None, lambda: None),
    MetaOptionSample("extensions", lambda: ("pg_trgm",), lambda: ("hstore",), cleared_value=()),
    MetaOptionSample(
        "triggers",
        lambda: [Trigger("trigger_base", on=TriggerEvent.INSERT, body=RawSQLTerm("SELECT 1"))],
        lambda: [Trigger("trigger_override", on=TriggerEvent.UPDATE, body=RawSQLTerm("SELECT 2"))],
        cleared_value=(),
    ),
    MetaOptionSample(
        "views",
        lambda: [View("view_base", query=RawSQLTerm("SELECT 1 AS one"))],
        lambda: [View("view_override", query=RawSQLTerm("SELECT 2 AS two"))],
        cleared_value=(),
    ),
    MetaOptionSample(
        "materialized_views",
        lambda: [MaterializedView("materialized_view_base", query=RawSQLTerm("SELECT 1 AS one"))],
        lambda: [MaterializedView("materialized_view_override", query=RawSQLTerm("SELECT 2 AS two"))],
        cleared_value=(),
    ),
    MetaOptionSample(
        "dictionaries",
        lambda: [Dictionary("dictionary_base", key=("id",), attributes=("name",))],
        lambda: [Dictionary("dictionary_override", key=("id",), attributes=("name",))],
        cleared_value=(),
    ),
    MetaOptionSample(
        "functions",
        lambda: [DatabaseFunction("function_base", returns="integer", body=RawSQLTerm("SELECT 1"), language="sql")],
        lambda: [
            DatabaseFunction("function_override", returns="integer", body=RawSQLTerm("SELECT 2"), language="sql")
        ],
        cleared_value=(),
    ),
    MetaOptionSample(
        "sequences",
        lambda: [DatabaseSequence("sequence_base", start=1)],
        lambda: [DatabaseSequence("sequence_override", start=2)],
        cleared_value=(),
    ),
    MetaOptionSample(
        "policies",
        lambda: [Policy("policy_base", command=PolicyCommand.SELECT, roles=("reader",), using=RawSQLTerm("true"))],
        lambda: [Policy("policy_override", command=PolicyCommand.SELECT, roles=("writer",), using=RawSQLTerm("true"))],
        cleared_value=(),
    ),
    MetaOptionSample(
        "grants",
        lambda: [Grant(privileges=(Privilege.SELECT,), roles=("reader",))],
        lambda: [Grant(privileges=(Privilege.SELECT,), roles=("writer",))],
        cleared_value=(),
    ),
    MetaOptionSample("row_level_security", lambda: RowLevelSecurity.ENABLED, lambda: RowLevelSecurity.FORCED),
    MetaOptionSample(
        "table_options",
        lambda: [SqliteTableOptions(strict=True)],
        lambda: [SqliteTableOptions(without_rowid=True)],
        cleared_value=(),
    ),
    MetaOptionSample("manager", lambda: Manager(), lambda: Manager()),
    MetaOptionSample("tenant_schema", lambda: True, lambda: False),
    MetaOptionSample("swappable", lambda: "CONTRACT_BASE_MODEL", lambda: "CONTRACT_OTHER_MODEL"),
    MetaOptionSample(
        "outbox_wakeup", lambda: InProcessWakeup(), lambda: None, base_model_path="hare.contrib.outbox.OutboxEvent"
    ),
    MetaOptionSample(
        "new_version_excluded_fields",
        lambda: ("name",),
        lambda: (),
        base_model_path="hare.contrib.versioning.versioned_model.VersionedModel",
    ),
)
