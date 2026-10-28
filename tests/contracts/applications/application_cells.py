"""The cells of the application matrix - each application of an integration, started by a test client in the
test's event loop or in a loop and thread of its own, under each of the plugin's two application fixtures."""

from tests.contracts.applications.application_cell import ApplicationCell

IN_THE_TESTS_LOOP = "the tests loop"
IN_A_THREAD = "a thread"
#: Litestar's AsyncTestClient - awaited in the test, it runs the application in the anyio portal's thread.
IN_A_PORTAL = "a portal"
APPLICATION_FIXTURES = ("hare_app_db", "hare_app_transactional_db")

#: Each application: the integration, how its client runs it, the file of its source and the extras it needs -
#: FastAPI's test clients run on httpx.
APPLICATIONS = (
    ("litestar", IN_A_PORTAL, "litestar_async_test_client", ("litestar",)),
    ("litestar", IN_A_THREAD, "litestar_in_a_thread", ("litestar",)),
    ("fastapi", IN_THE_TESTS_LOOP, "fastapi_in_the_tests_loop", ("fastapi", "http")),
    ("fastapi", IN_A_THREAD, "fastapi_in_a_thread", ("fastapi", "http")),
    ("robyn", IN_A_THREAD, "robyn_in_a_loop_of_its_own", ("robyn",)),
    ("taskiq", IN_THE_TESTS_LOOP, "taskiq_in_the_tests_loop", ("taskiq",)),
    ("taskiq", IN_A_THREAD, "taskiq_in_a_thread", ("taskiq",)),
)

APPLICATION_CELLS = tuple(
    ApplicationCell(integration, client, source_name, fixture, extras)
    for integration, client, source_name, extras in APPLICATIONS
    for fixture in APPLICATION_FIXTURES
)
#: The cells of each integration - one pytest run starts them all.
APPLICATION_CELLS_BY_INTEGRATION = {
    integration: tuple(cell for cell in APPLICATION_CELLS if cell.integration == integration)
    for integration in dict.fromkeys(cell.integration for cell in APPLICATION_CELLS)
}
