from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

from hare.instrumentation.capture.change_capturing import ChangeCapturing
from hare.instrumentation.enums import RowOperation

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.instrumentation.capture.capture_needs import CaptureNeeds
    from hare.models import Model
    from hare.models.write.instance_writer import InstanceWriter


class InstanceCapture:
    """The writes of one instance of a model with ``Meta.change_capture`` - its INSERT, UPDATE or
    DELETE and the captured change, in one transaction. The values come from the instance; a
    captured field it hasn't loaded is read from its row.
    """

    @staticmethod
    def get_capturing_writer(writer: InstanceWriter, connection: DatabaseClient) -> InstanceWriter:
        """A writer of the same model on ``connection`` that captures nothing itself.

        Args:
            writer: The capturing writer.
            connection: The connection - the write's transaction.

        Returns:
            The writer.
        """
        # Local import: the writer module imports this one.
        from hare.models.write.instance_writer import InstanceWriter

        inner_writer = InstanceWriter(writer.model, connection)
        inner_writer.captures_changes = False
        return inner_writer

    @staticmethod
    async def get_values(
        obj: Model, connection: DatabaseClient, capture_needs: CaptureNeeds
    ) -> tuple[dict[str, Any] | None, Any]:
        """The captured fields of an obj as its row holds them now, and its tenant.

        Args:
            obj: The obj.
            connection: The connection - the write's transaction.
            capture_needs: The model's needs.

        Returns:
            The values - None when the payload holds none - and the tenant.
        """
        if not capture_needs.reads_values:
            return None, ChangeCapturing.get_tenant(obj, capture_needs)
        values = ChangeCapturing.get_instance_values(obj, capture_needs)
        if values is not None:
            return values, ChangeCapturing.get_tenant(obj, capture_needs)
        values_by_pk = await ChangeCapturing.read_values(type(obj), connection, [obj.pk], capture_needs)
        return values_by_pk.get(obj.pk, (None, None))

    @staticmethod
    async def insert(writer: InstanceWriter, obj: Model, capture_needs: CaptureNeeds) -> None:
        """Inserts the obj's row and captures it.

        Args:
            writer: The model's writer.
            obj: The obj.
            capture_needs: The model's needs.
        """
        async with ChangeCapturing.transaction(writer.connection) as connection:
            await InstanceCapture.get_capturing_writer(writer, connection).execute_insert(obj)
            after, tenant = await InstanceCapture.get_values(obj, connection, capture_needs)
            model = type(obj)
            change = ChangeCapturing.build_change(
                model, capture_needs, RowOperation.INSERT, obj.pk, after=after, tenant=tenant
            )
            await ChangeCapturing.capture(connection, model, [change])

    @staticmethod
    def get_snapshot_values(obj: Model, capture_needs: CaptureNeeds) -> dict[str, Any] | None:
        """The captured fields as the obj was loaded or last saved - its dirty-field snapshot.

        Args:
            obj: The obj.
            capture_needs: The model's needs.

        Returns:
            The values, None when the snapshot lacks one of them.
        """
        snapshot: dict[str, Any] | None = getattr(obj, "_dirty_snapshot", None)
        if snapshot is None or not all(name in snapshot for name in capture_needs.field_names):
            return None
        return {name: snapshot[name] for name in capture_needs.field_names}

    @staticmethod
    async def update(
        writer: InstanceWriter,
        obj: Model,
        update_fields: Iterable[str] | None,
        capture_needs: CaptureNeeds,
        *,
        apply_active_tenant_scope_guard: bool,
        only_live_row: bool,
    ) -> int | None:
        """Updates the obj's row and captures it.

        Args:
            writer: The model's writer.
            obj: The obj.
            update_fields: The fields written, None for every writable one.
            capture_needs: The model's needs.
            apply_active_tenant_scope_guard: See ``InstanceWriter.execute_update()``.
            only_live_row: See ``InstanceWriter.execute_update()``.

        Returns:
            The number of matched rows, or None when there was nothing to write.
        """
        model = type(obj)
        update_fields = list(update_fields) if update_fields is not None else None
        if update_fields is not None:
            changed: list[str] | None = update_fields
        elif model._meta.track_dirty_fields:
            changed = list(obj.get_dirty_fields())
        else:
            changed = None
        async with ChangeCapturing.transaction(writer.connection) as connection:
            before = None
            if capture_needs.reads_before:
                before = InstanceCapture.get_snapshot_values(obj, capture_needs)
                if before is None:
                    values_by_pk = await ChangeCapturing.read_values(
                        model, connection, [obj.pk], capture_needs, lock=True
                    )
                    before = values_by_pk.get(obj.pk, (None, None))[0]
            rows = await InstanceCapture.get_capturing_writer(writer, connection).execute_update(
                obj,
                update_fields,
                apply_active_tenant_scope_guard=apply_active_tenant_scope_guard,
                only_live_row=only_live_row,
            )
            if rows:
                after, tenant = await InstanceCapture.get_values(obj, connection, capture_needs)
                change = ChangeCapturing.build_change(
                    model,
                    capture_needs,
                    RowOperation.UPDATE,
                    obj.pk,
                    changed=changed,
                    before=before,
                    after=after,
                    tenant=tenant,
                )
                await ChangeCapturing.capture(connection, model, [change])
        return rows

    @staticmethod
    async def delete(
        writer: InstanceWriter, obj: Model, capture_needs: CaptureNeeds, *, apply_active_tenant_scope_guard: bool
    ) -> int:
        """Deletes the obj's row and captures it - as the row was, read before the delete.

        Args:
            writer: The model's writer.
            obj: The obj.
            capture_needs: The model's needs.
            apply_active_tenant_scope_guard: See ``InstanceWriter.execute_delete()``.

        Returns:
            The number of matched rows.
        """
        model = type(obj)
        async with ChangeCapturing.transaction(writer.connection) as connection:
            values_by_pk = await ChangeCapturing.read_values(model, connection, [obj.pk], capture_needs, lock=True)
            rows = await InstanceCapture.get_capturing_writer(writer, connection).execute_delete(
                obj, apply_active_tenant_scope_guard=apply_active_tenant_scope_guard
            )
            if rows:
                before, tenant = values_by_pk.get(obj.pk, (None, None))
                change = ChangeCapturing.build_change(
                    model, capture_needs, RowOperation.DELETE, obj.pk, before=before, tenant=tenant
                )
                await ChangeCapturing.capture(connection, model, [change])
        return rows
