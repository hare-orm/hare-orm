from __future__ import annotations

from hare.models.enums import ModelOption

#: The share of a renamed model's fields that must still match by content for a rename that also
#: adds or removes a field. Conservative: a wrong pairing puts the old rows under the wrong schema.
MODEL_RENAME_FIELD_SIMILARITY_THRESHOLD = 0.5

#: Options of a new model its own operations add once every table of the migration exists - a view,
#: dictionary, function or policy may read any of them. Its sequences stay with ``CreateModel``: a column
#: default may take from one.
CREATE_MODEL_DEFERRED_OPTIONS = frozenset(
    {
        ModelOption.VIEWS,
        ModelOption.MATERIALIZED_VIEWS,
        ModelOption.DICTIONARIES,
        ModelOption.FUNCTIONS,
        ModelOption.POLICIES,
        ModelOption.GRANTS,
        ModelOption.ROW_LEVEL_SECURITY,
    }
)

#: Appended to an automatic M2M through table's name while it's moved aside, so a through model's
#: table (or the other way around) can take over the same name and receive its rows.
MANY_TO_MANY_THROUGH_TABLE_SWAP_SUFFIX = "__swap"
