#: Windows' ``SetProcessInformation()`` class of a process's power throttling state, and what of the
#: throttling is turned off: the execution speed (0x1) and ignoring the timer resolution (0x4).
POWER_THROTTLING_INFORMATION_CLASS = 4
POWER_THROTTLING_CONTROLS = 0x1 | 0x4
#: The marker of a test opening only SQLite databases of its own, whatever HARE_TEST_DB is - only the
#: suites of a SQLite database run it.
SQLITE_ONLY_MARKER = "sqlite_only"
#: The marker of a test of the models of tests.shared_models.testmodels_postgres - fields only
#: PostgreSQL's shared test database holds (a DecimalField): only its suites run it.
POSTGRESQL_MODELS_MARKER = "postgresql_models"
#: The marker of a test of exact decimals - a DecimalField or a Decimal value: only the suites of a
#: database storing them (not SQLite) run it.
DECIMAL_FIELDS_MARKER = "decimal_fields"
#: The marker of a test whose outcome depends on time - a timeout, a lease, a retry delay running out:
#: left out of every suite unless RUN_TIMING_OPTION asks for it.
TIMING_MARKER = "timing"
#: Runs the tests of TIMING_MARKER too.
RUN_TIMING_OPTION = "--run-timing"
