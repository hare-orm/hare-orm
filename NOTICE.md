# Third-party notices

[Русская версия](NOTICE.ru.md)

hare-orm is MIT-licensed (see [LICENSE](LICENSE)), but it began life as a fork of, and still
carries adapted code from, two Apache License 2.0 projects, and parts of its Rust PostgreSQL
driver are adapted from an MIT-licensed one. Both licenses allow this as long as the original
copyright and license notices are kept for the carried-over portions. That's what this file does.

## Tortoise ORM

hare-orm's model layer, migration system, and general API shape (`Model`, `QuerySet`, `Meta`,
field types, the CLI) started as a fork of [Tortoise ORM](https://github.com/tortoise/tortoise-orm).
Large parts have since been substantially rewritten or replaced (the query engine, the dialect
system, the Rust PostgreSQL driver, composite primary keys, versioning, soft delete, the
constraint/trigger system, and more), but code and design carried over from the original project
remains subject to its license:

```text
Copyright (c) Tortoise ORM contributors

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
```

## PyPika

`hare/sql/` (the internal query builder — `Query`, `Table`, `Field`, `Criterion`, the SQL
context a dialect renders with) is an adapted copy of [PyPika](https://github.com/kayak/pypika)'s query-construction
internals, extended with hare-orm-specific dialect and expression support. Also Apache License
2.0:

```text
Copyright (c) 2016 KAYAK Germany, GmbH

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
```

## yara-orm

The Rust PostgreSQL driver (`hare.dialects.postgresql.drivers.rust_pg`, `rust/native/src/pg/`) is
mostly hare-orm's own code. Its design was inspired by the Rust PostgreSQL backend of
[yara-orm](https://github.com/vsdudakov/yara-orm), an open-source ORM under the MIT License, and
these parts of it are adapted from yara-orm's code:

- in `rust/native/src/pg/client.rs` — the TLS connection setup (the certificate checks and the
  connector built from them) and the unification of parameter types across the rows of
  `execute_many`;
- in `rust/native/src/pg/value/` (`mod.rs`, `from_python.rs`, `into_python.rs`) — the base of the
  `Value` type: its variants and their conversion to and from Python objects;
- in `rust/native/src/pg/error.rs` — the driver's error type.

Those parts remain subject to its license:

```text
MIT License

Copyright (c) 2026 yara-orm contributors

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```
