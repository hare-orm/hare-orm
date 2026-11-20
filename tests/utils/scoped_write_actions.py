"""Equivalent row changes through public write paths of a scoped model."""

from hare.query.expressions import F
from tests.contracts.enums import ScopedWritePath


class ScopedWriteActions:
    @staticmethod
    async def change(model, key, increment, path):
        if path is ScopedWritePath.UPDATE:
            await model.objects.filter(pk=key).update(amount=F("amount") + increment)
        elif path is ScopedWritePath.DELETE:
            await model.objects.filter(pk=key).delete()
        else:
            obj = await model.objects.get(pk=key)
            obj.amount += increment
            if path is ScopedWritePath.SAVE:
                await obj.save(update_fields=["amount"])
            else:
                await model.objects.bulk_update([obj], ["amount"])
