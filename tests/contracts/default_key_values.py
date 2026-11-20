class DefaultKeyValues:
    """Changing keys expose repeated evaluation of one relation's default."""

    calls = 0
    returns_null = False
    key_container = tuple

    @staticmethod
    def get_key():
        DefaultKeyValues.calls += 1
        if DefaultKeyValues.returns_null:
            return None
        return DefaultKeyValues.key_container((DefaultKeyValues.calls, DefaultKeyValues.calls + 100))

    @staticmethod
    async def get_async_key():
        return DefaultKeyValues.get_key()

    @staticmethod
    def get_scalar_key():
        value = DefaultKeyValues.get_key()
        return None if value is None else value[0]

    @staticmethod
    async def get_async_scalar_key():
        return DefaultKeyValues.get_scalar_key()

    @staticmethod
    async def get_number():
        return 7

    @staticmethod
    async def get_token():
        return "00000000-0000-0000-0000-000000000001"
