# Первая модель

Пройдём весь путь от начала до конца: опишем модель, создадим её таблицу и выполним простые запросы.

## <a id="1-define-the-model"></a>1. Описываем модель

```python
# library/models.py
from hare import fields
from hare.models import Model


class Author(Model):
    id = fields.UUIDField(primary_key=True)
    name = fields.CharField(max_length=120)
    email = fields.CharField(max_length=254, unique=True, null=True)
    created_at = fields.DatetimeField(auto_now_add=True)

    class Meta:
        table_description = "Book authors"
        ordering = ("-created_at",)


class Book(Model):
    id = fields.UUIDField(primary_key=True)
    author = fields.ForeignKeyField("models.Author", related_name="books", on_delete=fields.CASCADE)
    title = fields.CharField(max_length=300)
    published_at = fields.DateField(null=True)
```

Модель — обычный класс, унаследованный от `hare.models.Model`. Поля — это атрибуты класса, а всё, что
относится к таблице целиком, — её имя, порядок строк, ограничения — задаётся во вложенном классе `Meta`.
Полное описание — в разделах [Типы полей](../models/field-types.ru.md) и
[Опции Meta](../models/meta-options.ru.md).

## <a id="2-initialize-hare-orm"></a>2. Подключаем hare

```python
# library/db.py
from hare import Hare, HareConfig

async def init_db() -> None:
    await Hare.init(HareConfig.from_db_url("sqlite+aiosqlite://db.sqlite3", {"models": ["library.models"]}))
    await Hare.generate_schemas()
```

`generate_schemas()` создаёт таблицы прямо по описаниям моделей — этого достаточно, чтобы быстро начать
или прогнать тесты. Для базы, которая будет работать на сервере, используйте миграции — см.
[Создание и применение миграций](../migrations/migrations.ru.md).

## <a id="3-create-and-query-rows"></a>3. Создаём строки и читаем их

```python
from library.models import Author, Book

async def run() -> None:
    author = await Author.objects.create(name="Ursula K. Le Guin", email="ukl@example.com")
    await Book.objects.create(author=author, title="The Left Hand of Darkness")
    await Book.objects.create(author=author, title="The Dispossessed")

    # все книги авторов, в имени которых есть «le guin», вместе с автором в том же запросе
    async for book in Book.objects.filter(author__name__icontains="le guin").select_related("author"):
        print(book.title, "—", book.author.name)

    # одна строка или None
    maybe = await Author.objects.get(email="ukl@example.com", does_not_exist_exception=None)

    # подсчёт, изменение, удаление — обычные возможности QuerySet
    total = await Book.objects.filter(author=author).count()
    await Book.objects.filter(title__istartswith="the").update(published_at=None)
    await author.delete()  # вместе с автором удалятся его книги: on_delete=CASCADE
```

Все методы `QuerySet` — `filter`, `annotate`, `values`, `bulk_create`, `prefetch_related` и остальные —
описаны в разделе [Методы QuerySet](../querying/queryset-methods.ru.md).

Дальше в документации такие операторы показаны без окружающего `async def`: каждый фрагмент выполняется
внутри `async`-кода с hare, инициализированным как на шаге 2, а модели импортируют
`from hare import fields` и `from hare.models import Model`.

## <a id="next"></a>Что дальше

- [Типы полей](../models/field-types.ru.md) — все классы полей и их аргументы.
- [Связи](../models/relations.ru.md) — внешние ключи, связи «один-к-одному» и «многие-ко-многим», связи с
  моделями, у которых составной первичный ключ.
- [Транзакции](../connections/transactions.ru.md) — `Transactions.atomic()`, `on_commit()` и
  `on_rollback()`.
- [Несколько баз данных](../connections/multiple-databases.ru.md) — `using=` и маршрутизаторы.
- [Если вы пришли с Django](coming-from-django.ru.md) — что называется иначе и где результаты отличаются.
