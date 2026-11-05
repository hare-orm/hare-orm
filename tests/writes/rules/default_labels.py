class DefaultLabels:
    """Defaults for a through model's encrypted fields."""

    @staticmethod
    def get_label() -> str:
        return "called label"

    @staticmethod
    async def get_async_label() -> str:
        return "awaited label"
