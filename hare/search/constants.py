from __future__ import annotations

#: The Features flag a text search configuration, a ``SearchVector`` value, lexemes, label weights,
#: rank normalization and the headline's fragment options need.
TEXT_SEARCH_CONFIGURATIONS_REQUIRED_FEATURE = "supports_text_search_configurations"

#: The Features flag weights by field of ``SearchRank`` need.
FULL_TEXT_INDEX_REQUIRED_FEATURE = "supports_full_text_index"

#: The largest weight of a field in ``SearchRank(weights={...})``.
SEARCH_RANK_MAX_FIELD_WEIGHT = 1_000_000.0

#: The longest marker or delimiter text of ``SearchHeadline``.
SEARCH_HEADLINE_MAX_MARKER_LENGTH = 1000

#: The largest word or fragment count of ``SearchHeadline``.
SEARCH_HEADLINE_MAX_COUNT = 100_000
