"""The write paths of the contract matrix - every way the ORM writes rows, each with what the writes report.
A path that is a method of hare's models, querysets or relations and isn't here fails the completeness
test."""

from __future__ import annotations

from tests.contracts.writes.enums import WriteRole as Role
from tests.contracts.writes.expected_change import ExpectedChange as Change
from tests.contracts.writes.write_path import WritePath
from tests.contracts.writes.write_path_actions import WritePathActions as Actions

#: What the soft delete of a subject reports besides the rows deleted: the cascade reaches the notes and the
#: many-to-many links update the tags.
SOFT_DELETE_REACH = (Change.update(Role.NOTE, unnamed=True), Change.update(Role.TAG, unnamed=True))
#: What the hard delete of a subject reports besides the rows deleted.
HARD_DELETE_REACH = (
    Change.delete(Role.NOTE, unnamed=True),
    Change.delete(Role.LINK, unnamed=True),
    Change.update(Role.TAG, unnamed=True),
)

WRITE_PATHS = (
    WritePath(
        "Model.save, new row",
        Actions.save_new_row,
        reported=(Change.insert(Role.SUBJECT, 3),),
        captured=(Change.insert(Role.SUBJECT, 3),),
        writes_amount=True,
    ),
    WritePath(
        "Model.save, changed row",
        Actions.save_changed_row,
        reported=(Change.update(Role.SUBJECT, 1),),
        captured=(Change.update(Role.SUBJECT, 1),),
        writes_amount=True,
    ),
    WritePath(
        "Model.save, update_fields",
        Actions.save_update_fields,
        reported=(Change.update(Role.SUBJECT, 1),),
        captured=(Change.update(Role.SUBJECT, 1),),
        writes_amount=True,
    ),
    WritePath(
        "Model.delete, soft",
        Actions.delete_instance,
        reported=(Change.update(Role.SUBJECT, 1), *SOFT_DELETE_REACH),
        captured=(Change.update(Role.SUBJECT, 1),),
    ),
    WritePath(
        "Model.hard_delete",
        Actions.hard_delete_instance,
        reported=(Change.delete(Role.SUBJECT, 1), *HARD_DELETE_REACH),
        captured=(Change.delete(Role.LINK, 1), Change.delete(Role.SUBJECT, 1)),
    ),
    WritePath(
        "Model.delete, soft, cascading to notes",
        Actions.delete_instance_with_notes,
        reported=(Change.update(Role.SUBJECT, 2), *SOFT_DELETE_REACH),
        captured=(Change.update(Role.SUBJECT, 2), Change.update(Role.NOTE, 1)),
    ),
    WritePath(
        "Model.hard_delete, cascading to notes",
        Actions.hard_delete_instance_with_notes,
        reported=(Change.delete(Role.SUBJECT, 2), *HARD_DELETE_REACH),
        captured=(Change.delete(Role.NOTE, 1), Change.delete(Role.SUBJECT, 2)),
    ),
    WritePath(
        "Model.restore",
        Actions.restore_instance,
        reported=(Change.update(Role.SUBJECT, 1),),
        captured=(Change.update(Role.SUBJECT, 1),),
        prepare=Actions.soft_delete_subject,
    ),
    WritePath(
        "QuerySet.create",
        Actions.create,
        reported=(Change.insert(Role.SUBJECT, 3),),
        captured=(Change.insert(Role.SUBJECT, 3),),
        writes_amount=True,
    ),
    WritePath(
        "QuerySet.get_or_create, creating",
        Actions.get_or_create_creating,
        reported=(Change.insert(Role.SUBJECT, 3),),
        captured=(Change.insert(Role.SUBJECT, 3),),
        writes_amount=True,
    ),
    WritePath("QuerySet.get_or_create, finding", Actions.get_or_create_finding, reported=(), captured=()),
    WritePath(
        "QuerySet.update_or_create, updating",
        Actions.update_or_create_updating,
        reported=(Change.update(Role.SUBJECT, 1),),
        captured=(Change.update(Role.SUBJECT, 1),),
        writes_amount=True,
    ),
    WritePath(
        "QuerySet.update_or_create, creating",
        Actions.update_or_create_creating,
        reported=(Change.insert(Role.SUBJECT, 3),),
        captured=(Change.insert(Role.SUBJECT, 3),),
        writes_amount=True,
    ),
    WritePath(
        "QuerySet.bulk_create",
        Actions.bulk_create,
        reported=(Change.insert(Role.SUBJECT, 3, 4),),
        captured=(Change.insert(Role.SUBJECT, 3, 4),),
        writes_amount=True,
    ),
    WritePath(
        "QuerySet.bulk_create, ignore_conflicts",
        Actions.bulk_create_ignoring_conflicts,
        reported=(Change.insert(Role.SUBJECT, 3),),
        captured=(Change.insert(Role.SUBJECT, 3),),
        writes_amount=True,
        required_features=("supports_insert_conflicts",),
    ),
    WritePath(
        "QuerySet.bulk_create, update_fields",
        Actions.bulk_create_upserting,
        reported=(Change.insert(Role.SUBJECT, 3), Change.update(Role.SUBJECT, 1)),
        captured=(Change.insert(Role.SUBJECT, 3), Change.update(Role.SUBJECT, 1)),
        writes_amount=True,
        required_features=("supports_insert_conflicts",),
    ),
    WritePath(
        "QuerySet.bulk_update",
        Actions.bulk_update,
        reported=(Change.update(Role.SUBJECT, 1, 2),),
        captured=(Change.update(Role.SUBJECT, 1, 2),),
        writes_amount=True,
    ),
    WritePath(
        "QuerySet.update",
        Actions.queryset_update,
        reported=(Change.update(Role.SUBJECT, 1, unnamed=True),),
        captured=(Change.update(Role.SUBJECT, 1),),
        writes_amount=True,
    ),
    WritePath(
        "QuerySet.delete, soft",
        Actions.queryset_delete,
        reported=(Change.update(Role.SUBJECT, 1, unnamed=True), *SOFT_DELETE_REACH),
        captured=(Change.update(Role.SUBJECT, 1),),
    ),
    WritePath(
        "QuerySet.hard_delete",
        Actions.queryset_hard_delete,
        reported=(Change.delete(Role.SUBJECT, 1, unnamed=True), *HARD_DELETE_REACH),
        captured=(Change.delete(Role.LINK, 1), Change.delete(Role.SUBJECT, 1)),
    ),
    WritePath(
        "QuerySet.restore",
        Actions.queryset_restore,
        reported=(Change.update(Role.SUBJECT, 1, unnamed=True),),
        captured=(Change.update(Role.SUBJECT, 1),),
        prepare=Actions.soft_delete_subject,
    ),
    WritePath(
        "QuerySet.insert_from",
        Actions.insert_from,
        reported=(Change.insert(Role.SUBJECT, 3, unnamed=True),),
        captured=(Change.insert(Role.SUBJECT, 3),),
        writes_amount=True,
    ),
    WritePath(
        "QuerySet.merge",
        Actions.merge,
        reported=(Change.insert(Role.SUBJECT, 3, unnamed=True), Change.update(Role.SUBJECT, 1, unnamed=True)),
        captured=(Change.insert(Role.SUBJECT, 3), Change.update(Role.SUBJECT, 1)),
        writes_amount=True,
        required_features=("supports_merge",),
    ),
    WritePath(
        "ReverseRelation.create",
        Actions.notes_create,
        reported=(Change.insert(Role.NOTE, 2),),
        captured=(Change.insert(Role.NOTE, 2),),
        writes_amount=True,
    ),
    WritePath(
        "ReverseRelation.bulk_create",
        Actions.notes_bulk_create,
        reported=(Change.insert(Role.NOTE, 2, 3),),
        captured=(Change.insert(Role.NOTE, 2, 3),),
        writes_amount=True,
    ),
    WritePath(
        "ReverseRelation.get_or_create",
        Actions.notes_get_or_create,
        reported=(Change.insert(Role.NOTE, 2),),
        captured=(Change.insert(Role.NOTE, 2),),
        writes_amount=True,
    ),
    WritePath(
        "ReverseRelation.update_or_create",
        Actions.notes_update_or_create,
        reported=(Change.update(Role.NOTE, 1),),
        captured=(Change.update(Role.NOTE, 1),),
        writes_amount=True,
    ),
    WritePath(
        "ReverseRelation.add",
        Actions.notes_add,
        reported=(Change.update(Role.NOTE, 1),),
        captured=(Change.update(Role.NOTE, 1),),
    ),
    WritePath(
        "ReverseRelation.remove",
        Actions.notes_remove,
        reported=(Change.update(Role.NOTE, 1),),
        captured=(Change.update(Role.NOTE, 1),),
    ),
    WritePath(
        "ReverseRelation.clear",
        Actions.notes_clear,
        reported=(Change.update(Role.NOTE, 1, unnamed=True),),
        captured=(Change.update(Role.NOTE, 1),),
    ),
    WritePath(
        "ReverseRelation.set",
        Actions.notes_set,
        reported=(Change.update(Role.NOTE, 1),),
        captured=(Change.update(Role.NOTE, 1),),
    ),
    WritePath(
        "ManyToManyRelation.add",
        Actions.tags_add,
        reported=(
            Change.update(Role.SUBJECT, 1),
            Change.update(Role.TAG, 2),
            Change.insert(Role.LINK, unnamed=True),
        ),
        captured=(Change.insert(Role.LINK, 2),),
    ),
    WritePath(
        "ManyToManyRelation.remove",
        Actions.tags_remove,
        reported=(
            Change.update(Role.SUBJECT, 1),
            Change.update(Role.TAG, 1),
            Change.delete(Role.LINK, unnamed=True),
        ),
        captured=(Change.delete(Role.LINK, 1),),
    ),
    WritePath(
        "ManyToManyRelation.clear",
        Actions.tags_clear,
        reported=(
            Change.update(Role.SUBJECT, 1),
            Change.update(Role.TAG, unnamed=True),
            Change.delete(Role.LINK, unnamed=True),
        ),
        captured=(Change.delete(Role.LINK, 1),),
    ),
    WritePath(
        "ManyToManyRelation.set",
        Actions.tags_set,
        reported=(
            Change.update(Role.SUBJECT, 1),
            Change.update(Role.TAG, 2),
            Change.insert(Role.LINK, unnamed=True),
            Change.update(Role.SUBJECT, 1),
            Change.update(Role.TAG, unnamed=True),
            Change.delete(Role.LINK, unnamed=True),
        ),
        captured=(Change.insert(Role.LINK, 2), Change.delete(Role.LINK, 1)),
    ),
    WritePath(
        "ManyToManyRelation.create",
        Actions.tags_create,
        reported=(
            Change.insert(Role.TAG, 3),
            Change.update(Role.SUBJECT, 1),
            Change.update(Role.TAG, 3),
            Change.insert(Role.LINK, unnamed=True),
        ),
        captured=(Change.insert(Role.LINK, 2),),
    ),
    WritePath(
        "ManyToManyRelation.get_or_create",
        Actions.tags_get_or_create,
        reported=(
            Change.insert(Role.TAG, 3),
            Change.update(Role.SUBJECT, 1),
            Change.update(Role.TAG, 3),
            Change.insert(Role.LINK, unnamed=True),
        ),
        captured=(Change.insert(Role.LINK, 2),),
    ),
    WritePath(
        "ManyToManyRelation.update_or_create",
        Actions.tags_update_or_create,
        reported=(
            Change.insert(Role.TAG, 3),
            Change.update(Role.SUBJECT, 1),
            Change.update(Role.TAG, 3),
            Change.insert(Role.LINK, unnamed=True),
        ),
        captured=(Change.insert(Role.LINK, 2),),
    ),
)
