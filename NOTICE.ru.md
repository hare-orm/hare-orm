# Уведомления о стороннем коде

[English](NOTICE.md)

hare-orm распространяется по лицензии MIT (см. [LICENSE](LICENSE)), но начиналась как форк двух
проектов под лицензией Apache 2.0 и до сих пор содержит переработанный код из них, а части её
драйвера PostgreSQL на Rust переработаны из проекта под лицензией MIT. Обе лицензии это разрешают
при условии, что для перенесённых частей сохранены исходные уведомления об авторских правах и
лицензии. Этот файл их и сохраняет.

Тексты уведомлений ниже приведены дословно, на языке оригинала.

## Tortoise ORM

Слой моделей hare-orm, система миграций и общий вид API (`Model`, `QuerySet`, `Meta`, типы полей,
командная строка) начинались как форк [Tortoise ORM](https://github.com/tortoise/tortoise-orm).
С тех пор большие части существенно переписаны или заменены (движок запросов, система диалектов,
драйвер PostgreSQL на Rust, составные первичные ключи, версии, мягкое удаление, система ограничений
и триггеров и многое другое), но код и решения, перенесённые из исходного проекта, остаются под
его лицензией:

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

`hare/sql/` (внутренний построитель запросов — `Query`, `Table`, `Field`, `Criterion`, контекст SQL,
которым рендерит диалект) — переработанная копия внутреннего устройства построения запросов
[PyPika](https://github.com/kayak/pypika), дополненная поддержкой диалектов и выражений hare-orm.
Тоже лицензия Apache 2.0:

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

Драйвер PostgreSQL на Rust (`hare.dialects.postgresql.drivers.rust_pg`, `rust/native/src/pg/`) —
в основном собственный код hare-orm. Его устройство создано по образцу бэкенда PostgreSQL на Rust
проекта [yara-orm](https://github.com/vsdudakov/yara-orm), открытой ORM под лицензией MIT, и эти его
части переработаны из кода yara-orm:

- в `rust/native/src/pg/client.rs` — настройка TLS-подключения (проверки сертификата и сборка
  подключения по ним) и приведение типов параметров к общему типу по всем строкам `execute_many`;
- в `rust/native/src/pg/value/` (`mod.rs`, `from_python.rs`, `into_python.rs`) — основа типа
  `Value`: его варианты и их преобразование в объекты Python и обратно;
- в `rust/native/src/pg/error.rs` — тип ошибок драйвера.

На эти части по-прежнему распространяется его лицензия:

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
