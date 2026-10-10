# Версии записей

`VersionedModel` из `hare.contrib.versioning` — основа для хранения версий записей, в которую только
добавляют: каждое изменение создаёт новую строку с тем же `id`, но большим `version`, а не меняет
строку на месте.

```python
class VersionedModel(Model):
    id = fields.UUIDField(default=uuid4, db_index=True)
    version = fields.PositiveSmallIntField(default=1)
    pk = CompositePrimaryKey("id", "version")

    class Meta:
        abstract = True

    @classmethod
    async def get_last_version_or_exception(
        cls, *args: Q, exception: type[Exception] | None = None, **kwargs: Any
    ) -> Self: ...

    def get_new_version(self, **kwargs: Any) -> Self: ...      # QueryError, если в kwargs есть "id"/"version"
    async def create_new_version(self, **kwargs: Any) -> Self: ...
```

`id` и `version` вместе — настоящий первичный ключ таблицы, а не искусственный ключ с добавленным
сбоку ограничением уникальности. Поэтому внешний ключ может ссылаться на конкретную версию настоящим,
проверяемым базой `FOREIGN KEY` (см.
[Связи — связь с составным первичным ключом](../models/relations.ru.md#targeting-a-composite-primary-key)).

```python
from hare import fields
from hare.contrib.versioning import VersionedModel


class Article(VersionedModel):
    title = fields.CharField(max_length=200)
    config = fields.JSONField(default=dict)

    class Meta:
        new_version_excluded_fields = ("published_at",)


current = await Article.get_last_version_or_exception(id=article_id)
draft = await current.create_new_version(title="Updated title")
```

`get_new_version`/`create_new_version` копируют каждое поле самой модели через `deepcopy`, кроме `id`,
`version` и всего, что перечислено в `Meta.new_version_excluded_fields`, и увеличивают `version` на 1.
Колонка, которую вычисляет база (`GeneratedField`, любое поле с `generated=True`), и колонка с
`auto_now=True` никогда не копируются — они получают свежее значение при записи новой версии. Прямую
связь (внешний ключ или «один-к-одному») можно перечислить или переопределить по имени связи (`owner`)
или по её колонке ключа (`owner_id`) — в обоих случаях колонка ключа из старой версии не копируется.
Объект, загруженный через `.only()`/`.defer()`, даёт `IncompleteInstanceError` с именами незагруженных
полей, если они не переданы как переопределения; колонки, которые вычисляет база, и колонки
`auto_now` загружать не нужно никогда.

С [`Meta.soft_delete_field`](soft-delete.ru.md) новая версия никогда не рождается удалённой — это поле
не копируется, — а `get_last_version_or_exception()` ищет наибольшую версию и среди мягко удалённых
строк, так что следующая версия никогда не совпадает с удалённой.

Объединяйте `VersionedModel` с базовой моделью своего проекта через множественное наследование:

```python
class AppVersionedModel(YourBaseModel, VersionedModel):
    class Meta(YourBaseModel.Meta, VersionedModel.Meta):
        pass
```

> [!NOTE]
> **Чего здесь нет**
>
> Запросов «состояние на момент версии X», сравнения версий и автоматической очистки старых версий
> нет — добавьте их сверху, если нужно.

> [!WARNING]
> **Поле с `unique=True` конфликтует с хранением версий**
>
> Каждая версия — настоящая постоянная строка: `create_new_version()` никогда не удаляет и не
> перезаписывает предыдущую. Поле с `unique=True` (или `UniqueConstraint` в `Meta.constraints`),
> которое не сбрасывается через `Meta.new_version_excluded_fields`, сохраняет в новой строке старое
> значение, и вторая версия сталкивается с первой на уровне базы:
>
> ```python
> class Article(VersionedModel):
>     slug = fields.CharField(max_length=200, unique=True)
>     title = fields.CharField(max_length=200)
>
>
> article = await Article.objects.create(slug="my-article", title="Draft")
> await article.create_new_version(title="Revised")  # IntegrityError: slug уже существует
> ```
>
> Полю, которое обозначает один и тот же документ во всех его версиях (слаг, внешний ключ другой
> системы), нужна иная уникальность, чем простое `unique=True`: например, колонка-признак текущей
> версии и частичное `UniqueConstraint(fields=("slug",), condition=Q(is_current=True))`, или отказ от
> уникальности в базе и проверка «один действующий слаг» в приложении.
