# SQLite: connecting

How hare opens a SQLite database: the URL and its parameters, the databases a connection attaches, and
the faults of particular SQLite libraries it leaves to the library.

## <a id="sqlite-connection-parameters"></a>Connection parameters

A SQLite `db_url` path is percent-decoded like a PostgreSQL database name
(`sqlite+aiosqlite://my%20db.sqlite` → `my db.sqlite`). Besides the PRAGMAs below, a query parameter or
`credentials` key may be one of hare's own settings: `attached_databases` ([Attached
databases](#attached-databases)), `install_regexp_functions` ([Regular
expressions](functions.md#regular-expressions)), `load_sqlite_vec` ([sqlite-vec](../search-and-geodata/vector-search.md#sqlite)),
`load_spatialite`, `spatialite_path`, `spatialite_proj_database` and `spatialite_metadata`
([SpatiaLite](../search-and-geodata/gis.md#spatialite)), and `connect_max_retries`/`connect_retry_backoff_base_seconds`,
taken and unused so a `DB_URL` shared with another database still works. Every other one sets a
connection PRAGMA, and only these are accepted, each value checked before anything is sent (a
PRAGMA can't take a bind parameter):

| Type | PRAGMAs | Values |
|---|---|---|
| keyword | `journal_mode`, `synchronous`, `temp_store`, `locking_mode`, `auto_vacuum`, `secure_delete` | the keywords SQLite documents for each (`journal_mode`: `DELETE`/`TRUNCATE`/`PERSIST`/`MEMORY`/`WAL`/`OFF`), case-insensitive |
| boolean | `foreign_keys`, `case_sensitive_like`, `recursive_triggers`, `automatic_index`, `cell_size_check`, `defer_foreign_keys`, `ignore_check_constraints`, `trusted_schema`, `reverse_unordered_selects` | `true`/`false`, `1`/`0`, `yes`/`no`, `on`/`off` |
| whole number | `journal_size_limit` (`-1`..`2**40`), `cache_size`, `busy_timeout` (`0`..`86400000` ms), `mmap_size`, `page_size` (a power of two, `512`..`65536`), `wal_autocheckpoint`, `threads` (`0`..`64`), `max_page_count`, `cache_spill` | within the range |

Any other key (a typo such as `jurnal_mode`) or an unaccepted value raises `ConfigurationError`, as does
an `install_regexp_functions` value that isn't a boolean. A connection without `file_path` raises
`ConfigurationError` too.

`page_size` and `auto_vacuum` shape the database file, so SQLite takes them only before the file's header
is written: hare sends them first, before `journal_mode=WAL`, and they take effect on a new file. An
existing file keeps its own until `VACUUM`.

`timezone` is checked at `Hare.init()` — an unknown zone name raises `ConfigurationError` right away
instead of failing on the first datetime value.

## <a id="attached-databases"></a>Attached databases

SQLite has no schemas: a model's `Meta.schema` names an attached database, and every statement writes
the model's table in it — `"archive"."article"`. Each connection attaches the databases its
`attached_databases` setting names (`ATTACH DATABASE`), before the first statement and again after every
reconnect:

```python
# credentials: a name to its file
{"file_path": "main.db", "attached_databases": {"archive": "archive.db", "scratch": ":memory:"}}
```

```text
sqlite+aiosqlite://main.db?attached_databases=archive:archive.db,scratch::memory:
```

In a URL each `name:file` pair is split on its first colon, the pairs on commas. A name SQLite keeps for
itself (`main`, `temp`), an empty name or file, a name given twice, or a value of no such pairs raises
`ConfigurationError`. A
model of a database the connection doesn't attach meets SQLite's own `unknown database` error; migrations
create no database (`Features.supports_schemas` is false).

The DDL is written as SQLite takes it for an attached database: an index or trigger is named in the
database (`CREATE INDEX "archive"."ix" ON "article" (...)`) and its table is one of the same database; a
foreign key references a table of its own database, so a relation with `db_constraint=True` to a model of
another schema is refused when the model is bound (`Features.references_other_schemas` is false) —
declare it `db_constraint=False`. A `FullTextIndex` keeps its FTS5 table and triggers in the model's
database. SpatiaLite registers and indexes the geometry columns of the main database only: a
`SpatialiteIndex` of a model of an attached database raises `UnSupportedError`.

## <a id="sqlite-library-faults"></a>Library faults

hare doesn't work around a fault of a particular SQLite library — the library's own result stands:

- SQLite 3.38.0 to 3.41.0 build automatic indexes that ignore the collating sequence of the comparison
  they serve, so a comparison under a collation (`Collate()`) over a joined table can find no rows. On
  these libraries turn automatic indexes off yourself with `automatic_index=False` in the connection's
  settings. SQLite 3.41.1 fixed it.
- SQLite 3.38.0 to 3.38.5 report a foreign key broken by a statement with `RETURNING` as a plain error
  instead of a constraint failure — hare raises it as `OperationalError` there, not `IntegrityError`.
  SQLite 3.39.0 fixed it.
