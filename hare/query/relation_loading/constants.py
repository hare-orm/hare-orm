from __future__ import annotations

#: The annotation a many-to-many prefetch read in one query carries each related row's owner key
#: in - removed from the instances once they are laid out.
PREFETCH_OWNER_KEY_ANNOTATION = "hare_prefetch_owner_key"

#: The annotation numbering the rows of a sliced Prefetch queryset within each parent's rows.
PREFETCH_ROW_NUMBER_ANNOTATION = "hare_prefetch_row_number"
