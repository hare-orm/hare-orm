#: Windows' ``SetProcessInformation()`` class of a process's power throttling state, and what of the
#: throttling is turned off: the execution speed (0x1) and ignoring the timer resolution (0x4).
POWER_THROTTLING_INFORMATION_CLASS = 4
POWER_THROTTLING_CONTROLS = 0x1 | 0x4
#: The marker of a test opening only SQLite databases of its own, whatever HARE_TEST_DB is - only the
#: suites of a SQLite database run it.
SQLITE_ONLY_MARKER = "sqlite_only"
#: The marker of a test whose outcome depends on time - a timeout, a lease, a retry delay running out:
#: left out of every suite unless RUN_TIMING_OPTION asks for it.
TIMING_MARKER = "timing"
#: Runs the tests of TIMING_MARKER too.
RUN_TIMING_OPTION = "--run-timing"
