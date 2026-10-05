"""hare's pytest plugin - fixtures making the test databases of the configuration and isolating each
test, loaded by pytest itself once hare is installed (``hare-orm[pytest]`` adds pytest-asyncio).

Example::

    # pyproject.toml
    [tool.pytest.ini_options]
    hare_config = "myapp.settings.HARE_ORM"
    asyncio_default_fixture_loop_scope = "session"
    asyncio_default_test_loop_scope = "session"


    # a test
    @pytest.mark.asyncio
    async def test_signup(hare_db):
        await User.objects.create(name="ann")
"""
