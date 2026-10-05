# Если вы пришли с Django

Модели, запросы и миграции hare устроены так же, как в Django, поэтому почти всё, что знает разработчик
на Django, остаётся в силе. На этой странице — что называется иначе, что работает так же и где hare даёт
другой результат. Остальная документация описывает hare саму по себе.

## <a id="names"></a>Что называется иначе

| Django | hare |
|---|---|
| `from django.db import models`, `models.Model` | `from hare import fields`, `from hare.models import Model` |
| `models.ForeignKey`, `models.OneToOneField`, `models.ManyToManyField` | `fields.ForeignKeyField`, `fields.OneToOneField`, `fields.ManyToManyField` — см. [Связи](../models/relations.ru.md) |
| `GenericForeignKey` из `contenttypes` | `fields.GenericForeignKeyField` |
| `Meta.db_table`, `Meta.app_label`, `Meta.db_table_comment` | `Meta.table`, `Meta.app`, `Meta.table_description` — см. [Опции Meta](../models/meta-options.ru.md) |
| `Meta.unique_together` | `UniqueConstraint(fields=(...))` в `Meta.constraints` |
| `settings.DATABASES`, `DATABASE_ROUTERS` | `connections` и `routers` в [`Hare.init()`](../connections/configuration.ru.md#hare-init) |
| `transaction.atomic()`, `transaction.on_commit()` | `Transactions.atomic()`, `Transactions.on_commit()` — см. [Транзакции](../connections/transactions.ru.md) |
| `ATOMIC_REQUESTS` | `atomic_requests` интеграции с веб-фреймворком — см. [Веб-фреймворки](../integrations/web-frameworks.ru.md#atomic-requests) |
| `manage.py makemigrations`, `migrate`, `sqlmigrate`, `squashmigrations`, `inspectdb` | `hare makemigrations`, `hare migrate`, `hare sqlmigrate`, `hare squashmigrations`, `hare inspectdb` — см. [Команда `hare`](../migrations/cli.ru.md) |
| `django.utils.timezone.override()` | `Timezone.override()` (`from hare.time import Timezone`) |

Каждый запрос и каждая запись — корутина: `await Book.objects.filter(...)`, `async for book in ...`,
`await book.save()`. `on_delete` по умолчанию — `CASCADE`.

## <a id="same-behavior"></a>Что работает так же

- Цепочка маршрутизаторов: побеждает первый, вернувший псевдоним, а чтение, от которого зависит запись
  (`get_or_create()`, `update_or_create()`, `select_for_update()`), идёт через подключение для записи.
- `on_commit()`, вызванный после окончания транзакции, выполняет колбэк сразу.
- Путь к полю может заканчиваться связью или `pk`; `relation=None` у связи «ко многим» — это
  `relation__isnull=True`; оператор по имени самой связи сравнивает её ключ.
- Неявный `GROUP BY` у `values().annotate()`, и агрегат, добавленный до `values()`, считается для каждой
  строки модели.
- `update_fields=["author"]` записывает `author_id`; `ForeignKeyField` создаёт индекс по колонке ключа;
  `add()`, `remove()` и `set()` связи «многие-ко-многим» принимают объекты или значения ключа.
- `Index(fields=("-created_at",))` — ключ по убыванию; условие ограничения — это `Q`.
- Запрос со срезом нельзя фильтровать, пересортировать или сделать `distinct()`; агрегировать можно.

## <a id="different-results"></a>Где hare даёт другой результат

- **`exclude()` через связь «ко многим».** Условия одного вызова `exclude()` относятся к одной и той же
  связанной строке, как в `filter()`: `exclude(books__name="a", books__rating=5)` оставляет автора, у
  которого одна книга называется «a», а другая имеет оценку 5. Django отбрасывает и его; в hare для этого
  нужны два вызова — `.exclude(books__name="a").exclude(books__rating=5)`. См.
  [`exclude()`](../querying/queryset-methods.ru.md#exclude).
- **`filter()` и `annotate()` в любом порядке.** `annotate(n=Count("books")).filter(books__price__gt=6)`
  использует `JOIN` агрегата и считает только подходящие книги — так же, как `filter(...).annotate(...)`.
  В Django от порядка число меняется. См. [Агрегаты и фильтры по связям «ко
  многим»](../querying/aggregation.ru.md#aggregates-to-many).
- **Агрегаты по двум связям «ко многим».** Сумма или число, которые второй `JOIN` «ко многим» завысил бы,
  дают `QueryError`, а не завышенное значение, которое возвращает Django.
- **`distinct()` с сортировкой по невыбранной колонке.** hare возвращает каждое сочетание выбранных
  колонок один раз; Django убирает повторы и по колонкам сортировки и может повторить выбранное значение.
  См. [`values()` и `values_list()`](../querying/values.ru.md).
- **`get()` у `union()`.** Принимает условия и применяет их к каждой ветке; Django их не принимает.
