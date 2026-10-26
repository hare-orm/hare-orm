from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from hare.core.hare_context import HareContext
from hare.models import Model
from tests.contracts.constants import CONTRACT_APP
from tests.contracts.writes.enums import WriteRole
from tests.contracts.writes.write_model_set import WriteModelSet
from tests.contracts.writes.write_observer import WriteObserver


class WriteScenario:
    """The database of one write observer: its tables, and the rows every write path starts from - subjects
    1 and 2 (the second with a note), tags 1 and 2, the first subject linked to the first tag.

    Attributes:
        after_fetch: Called with each row ``fetch()`` reads, before the path writes it - an observer
            changes the row behind the path's back here.
    """

    def __init__(self, context: HareContext, models: WriteModelSet, observer: WriteObserver) -> None:
        self.context = context
        self.models = models
        self.observer = observer
        self.after_fetch: Callable[[Model], Awaitable[None]] | None = None

    @property
    def subject(self) -> type[Model]:
        """The model most paths write."""
        return self.models.subject

    @property
    def note(self) -> type[Model]:
        """The model pointing at the subject."""
        return self.models.note

    @property
    def tag(self) -> type[Model]:
        """The model the subject has many of."""
        return self.models.tag

    async def fetch(self, model: type[Model], pk: int, *, include_deleted: bool = False) -> Model:
        """Reads a row the way a path starts from it, whatever the tenant it is of.

        Args:
            model: The model.
            pk: The key.
            include_deleted: Whether a soft-deleted row is read too.

        Returns:
            The row.
        """
        queryset = model.objects.all_tenants() if model._meta.tenant_field else model.objects.all()
        if include_deleted:
            queryset = queryset.include_deleted()
        row = await queryset.get(pk=pk)
        if self.after_fetch is not None:
            await self.after_fetch(row)
        return row

    def value(self, amount: int) -> Any:
        """The value a path writes to the ``amount`` field - the type of the field.

        Args:
            amount: The amount.

        Returns:
            The value.
        """
        return self.observer.get_amount_value(amount)

    async def fetch_all(self, model: type[Model]) -> list[Model]:
        """Reads every row of a model the way a path starts from them.

        Args:
            model: The model.

        Returns:
            The rows, by key.
        """
        queryset = model.objects.all_tenants() if model._meta.tenant_field else model.objects.all()
        rows = list(await queryset.order_by("pk"))
        if self.after_fetch is not None:
            for row in rows:
                await self.after_fetch(row)
        return rows

    async def get_snapshot(self) -> dict[str, dict[Any, dict[str, Any]]]:
        """Every row of the subject, the note and the link, soft-deleted and of every tenant included.

        Returns:
            Each model's rows by key, as their columns.
        """
        snapshot: dict[str, dict[Any, dict[str, Any]]] = {}
        for role in (WriteRole.SUBJECT, WriteRole.NOTE, WriteRole.LINK):
            model = self.models.get_model(role)
            queryset = model.objects.all_tenants() if model._meta.tenant_field else model.objects.all()
            if model._meta.soft_delete_field:
                queryset = queryset.include_deleted()
            snapshot[role.value] = {row["id"]: row for row in await queryset.order_by("pk").values()}
        return snapshot

    async def prepare(self) -> None:
        """Creates the tables and the starting rows."""
        self.context.register_live_models(self.models.get_models(), CONTRACT_APP, "models")
        await self.context.generate_schemas(safe=True)
        with self.observer.get_scope():
            first_tag = await self.tag.objects.create(id=1, name="t1")
            await self.tag.objects.create(id=2, name="t2")
            first_subject = await self.subject.objects.create(id=1, name="a", amount=self.value(10))
            second_subject = await self.subject.objects.create(id=2, name="b", amount=self.value(20))
            await self.note.objects.create(id=1, name="n1", amount=self.value(1), subject=second_subject)
            await first_subject.tags.add(first_tag)
