# SQLite: connecting

How hare opens a SQLite database: the URL and its parameters, and the faults of particular SQLite
libraries hare works around.

## <a id="sqlite-connection-parameters"></a>Connection parameters

A SQLite `db_url` path is percent-decoded like a PostgreSQL database name
(`sqlite+aiosqlite://my%20db.sqlite` → `my db.sqlite`). Every other query parameter or `credentials` key
sets a connection PRAGMA, and only these are accepted, each value checked before anything is sent (a
PRAGMA can't take a bind parameter):

| Type | PRAGMAs | Values |
|---|---|---|
| keyword | `journal_mode`, `synchronous`, `temp_store`, `locking_mode`, `auto_vacuum`, `secure_delete` | the keywords SQLite documents for each (`journal_mode`: `DELETE`/`TRUNCATE`/`PERSIST`/`MEMORY`/`WAL`/`OFF`), case-insensitive |
| boolean | `foreign_keys`, `case_sensitive_like`, `recursive_triggers`, `automatic_index`, `cell_size_check`, `defer_foreign_keys`, `ignore_check_constraints`, `trusted_schema`, `reverse_unordered_selects` | `true`/`false`, `1`/`0`, `yes`/`no`, `on`/`off` |
| whole number | `journal_size_limit` (`-1`..`2**40`), `cache_size`, `busy_timeout` (`0`..`86400000` ms), `mmap_size`, `page_size` (a power of two, `512`..`65536`), `wal_autocheckpoint`, `threads` (`0`..`64`), `max_page_count`, `cache_spill` | within the range |

Any other key (a typo such as `jurnal_mode`) or an unaccepted value raises `ConfigurationError`, as does
an `install_regexp_functions` value that isn't a boolean, and `automatic_index` turned on with SQLite
3.38.0 to 3.41.0, whose automatic indexes ignore a comparison's collating sequence (these turn it off by
default — see [Library faults](#sqlite-library-faults)). A connection without `file_path` raises
`ConfigurationError` too.

`page_size` and `auto_vacuum` shape the database file, so SQLite takes them only before the file's header
is written: hare sends them first, before `journal_mode=WAL`, and they take effect on a new file. An
existing file keeps its own until `VACUUM`.

`timezone` is checked at `Hare.init()` — an unknown zone name raises `ConfigurationError` right away
instead of failing on the first datetime value.

## <a id="sqlite-library-faults"></a>Library faults

Two faults of particular SQLite libraries are worked around by the library's version:

- SQLite 3.38.0 to 3.41.0 build automatic indexes that ignore the collating sequence of the comparison
  they serve, so a decimal or time comparison over a joined table — hare compares both under collations
  of its own — finds no rows. A connection on these libraries runs with `PRAGMA automatic_index = OFF`,
  and `automatic_index=True` in its settings raises `ConfigurationError`. SQLite 3.41.1 fixed it.
- SQLite 3.38.0 to 3.38.5 report a foreign key broken by a statement with `RETURNING` as a plain error
  instead of a constraint failure. hare raises it as `IntegrityError`, as on every other version. SQLite
  3.39.0 fixed it.
