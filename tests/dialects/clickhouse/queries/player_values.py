from typing import Any

from tests.dialects.clickhouse.models import Player


class PlayerValues:
    """Read expression values on the shared ClickHouse Player model in stable row order."""

    @staticmethod
    async def get(expression: Any, **filters: Any) -> list[Any]:
        return (
            await Player.objects.filter(**filters)
            .order_by("id")
            .annotate(value=expression)
            .values_list("value", flat=True)
        )
