from __future__ import annotations

#: The attributes a write sets on each object whose row it wrote - that it is saved, and the connection it
#: was written on - in this order.
SAVED_OBJECT_ATTRIBUTE_NAMES = ["_saved_in_db", "_connection_alias"]
#: The writing method a save names in its messages.
SAVE_METHOD_NAME = "save(update_fields=...)"
#: The writing method a bulk update names in its messages.
BULK_UPDATE_METHOD_NAME = "bulk_update()"
#: The writing method a bulk insert names in its messages.
BULK_CREATE_METHOD_NAME = "bulk_create()"
#: The arguments of a bulk insert's conflict handling, named in its messages.
BULK_CREATE_UPDATE_FIELDS_NAME = "bulk_create() update_fields"
BULK_CREATE_ON_CONFLICT_NAME = "bulk_create() on_conflict"
#: The writing method a many-to-many relation names in its messages.
ADD_METHOD_NAME = "add()"
REMOVE_METHOD_NAME = "remove()"
#: The argument the extra fields of a many-to-many relation's through rows are given in.
THROUGH_DEFAULTS_METHOD_NAME = "add(through_defaults=...)"
#: The writing method an update of an existing row by get-or-create names in its messages.
UPDATE_OR_CREATE_METHOD_NAME = "update_or_create(defaults=...)"
#: The writing method an update of a queryset names in its messages.
UPDATE_METHOD_NAME = ".update()"
#: The branches of a merge, named in their messages.
MERGE_UPDATE_METHOD_NAME = "merge(update=...)"
MERGE_INSERT_METHOD_NAME = "when_not_matched(insert=...)"
#: The writing method an insert of selected rows names in its messages.
INSERT_FROM_METHOD_NAME = "insert_from()"
#: The rows a write returns, named in its messages.
RETURNING_METHOD_NAME = "returning()"
#: A field a write refuses to set - ``reason`` says why.
REFUSED_FIELD_MESSAGE = "Cannot set {name!r} via {method} - {reason}"
#: The reasons of ``REFUSED_FIELD_MESSAGE``.
PRIMARY_KEY_REASON = "it is the primary key identifying the row"
GENERATED_REASON = "it is computed by the database, not written to"
VERSION_REASON = "it is Meta.optimistic_lock_field, bumped automatically"
SOFT_DELETE_REASON = "it is Meta.soft_delete_field - use .delete()/.restore() instead"
CONFLICT_TENANT_REASON = (
    "it is Meta.tenant_field - ON CONFLICT DO UPDATE resolves against the table's physical unique constraint "
    "regardless of tenant scoping, so this would move an existing row into another tenant"
)
BLIND_INDEX_REASON = "it is the blind index of {source!r} - it is written from that field"
COMPOSITE_RELATION_REASON = "it is a relation to a composite key - name its key fields ({fields}) instead"
SOURCE_ROWS_BLIND_INDEX_REASON = (
    "it has a blind index, computed from the plaintext in Python - the source rows are written in the database "
    "without passing through Python; write such rows with bulk_create()"
)
UNCOMPUTED_BLIND_INDEX_REASON = (
    "it has a blind index, computed from the plaintext in Python - give a plain value, or F() of another "
    "encrypted field with a blind index"
)
#: A name that is no field the write can set.
UNKNOWN_FIELD_MESSAGE = (
    "Unknown field {name!r} in {method} for model {model} - expected a field stored in its table or a forward relation"
)
#: A value a write of objects can't bind as a parameter - ``value_type`` names what the field holds.
UNBOUND_VALUE_MESSAGE = (
    "{method} on {model} writes values, but {fields} hold {value_type} - use QuerySet.update() to apply an "
    "expression to the written rows, or save() on each object; give a DatabaseDefault field a value first"
)
#: An update of an object whose version (``Meta.optimistic_lock_field``) the database gave and the object never read.
UNREAD_VERSION_MESSAGE = (
    "Cannot update {model} - its version {field!r} holds the DatabaseDefault sentinel, the value the database gave "
    "it never read back: refresh_from_db() the object, or insert it with bulk_create(returning=True)"
)
