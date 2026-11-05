from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, cast

from hare.fields.relations.relation_accessors import RelationAccessors
from hare.query.relation_loading.prefetching.declarations import PrefetchAttachment
from hare.query.relation_loading.prefetching.enums import PrefetchAttachmentType

if TYPE_CHECKING:
    from hare.models import Model


class PrefetchResult:
    """Fetched attachments held separately from the existing model graph.

    Fetchers only append here. The enclosing plan attaches all successful results after
    every task has completed. A failed or cancelled fetch discards its results and drains
    sibling tasks before returning control to the caller.
    """

    __slots__ = ("attachments",)

    def __init__(self) -> None:
        self.attachments: list[PrefetchAttachment] = []

    def add_forward(self, obj: Model, field: str, to_attribute: str | None, value: Model | None) -> None:
        self.attachments.append(PrefetchAttachment(obj, field, to_attribute, value, PrefetchAttachmentType.FORWARD))

    def add_reverse(self, obj: Model, field: str, to_attribute: str | None, value: Model | None) -> None:
        self.attachments.append(PrefetchAttachment(obj, field, to_attribute, value, PrefetchAttachmentType.REVERSE))

    def add_many(self, obj: Model, field: str, to_attribute: str | None, value: list[Model]) -> None:
        self.attachments.append(PrefetchAttachment(obj, field, to_attribute, value, PrefetchAttachmentType.MANY))

    def extend(self, result: PrefetchResult) -> None:
        self.attachments.extend(result.attachments)

    def take_slice(self, row_slice: slice) -> None:
        """Applies a per-parent slice before any relation cache becomes visible."""
        self.attachments = [
            replace(attachment, value=cast("list[Model]", attachment.value)[row_slice])
            if attachment.attachment_type is PrefetchAttachmentType.MANY
            else attachment
            for attachment in self.attachments
        ]

    def attach(self) -> None:
        """Publishes results synchronously; no fetch can still mutate these attachments."""
        for attachment in self.attachments:
            obj, field, value = attachment.obj, attachment.field, attachment.value
            if attachment.to_attribute:
                setattr(obj, attachment.to_attribute, value)
            elif attachment.attachment_type is PrefetchAttachmentType.MANY:
                RelationAccessors.get_relation(obj, field)._set_result_for_query(cast("list[Model]", value), None)
            elif attachment.attachment_type is PrefetchAttachmentType.REVERSE or value is None:
                setattr(obj, f"_{field}", value)
            else:
                setattr(obj, field, value)
