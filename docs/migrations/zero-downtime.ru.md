# Миграции без остановки приложения (расширение, затем сжатие)

`BackfillColumn` и `AlterColumnNotNullSafe` (`hare.migrations.operations`) разбивают изменение схемы,
которое иначе держало бы блокировку или транзакцию всё время обработки каждой строки большой таблицы,
на несколько маленьких, по отдельности безопасных миграций — приём «расширение, затем сжатие»
(expand-contract).

```python
BackfillColumn(model_name: str, field_name: str, value: Any | Callable[[], Any], *, batch_size: int = 1000)
AlterColumnNotNullSafe(model_name: str, field_name: str)
```

`BackfillColumn` выполняет цикл ограниченных `UPDATE` (не больше `batch_size` строк за команду, пока
строки не закончатся) вместо одного огромного `UPDATE` по всей таблице. `value` — значение или функция
без аргументов; функция вычисляется **один раз** (а не для каждой строки), поэтому она нужна для
«значения этого запуска миграции» (например, времени запуска), а не для значения, своего у каждой
строки, — ссылаться на другую колонку она не может. Операция меняет только данные, поэтому
`state_forward()` ничего не делает, и откатить её по смыслу нельзя (перезаписанные `NULL` не
восстановить) — `migrate` к более ранней миграции отказывается её откатывать. `field_name` может называть
внешний ключ именем связи (`"author"`) или полем-колонкой (`"author_id"`); тогда `value` — первичный
ключ связанной строки (принимается и объект модели). Имя без одной колонки в базе даёт
`ConfigurationError`.

`AlterColumnNotNullSafe` выполняет тот же `ALTER COLUMN ... SET NOT NULL`, что и `AlterField`, но
сначала проверяет, не осталось ли в колонке `NULL`, и даёт `hare.exceptions.ConfigurationError` — с
именами модели и поля и советом использовать `BackfillColumn`, — вместо того чтобы пропустить ошибку
нарушения `NOT NULL` прямо из базы. В PostgreSQL выполнение этой миграции с `atomic=True` (по
умолчанию) закрывает промежуток между проверкой и `ALTER`: перед проверкой берётся
`LOCK TABLE ... IN SHARE ROW EXCLUSIVE MODE` до конца транзакции миграции, поэтому параллельная запись
не может вставить новый `NULL` в промежутке. С `atomic=False` или в SQLite (там нет такой блокировки
таблицы) этот промежуток реален — выполняйте операцию в окно обслуживания или сначала добавьте
ограничение `CHECK (col IS NOT NULL)`, если нужна полная гарантия независимо от атомарности.

## Приём 1: обязательная колонка в большой таблице {: #adding-a-required-column }

Если разбить это на три отдельные миграции, ни одна из них не будет блокировать таблицу всё время
массового заполнения:

```python
# 0004_add_status_nullable.py
class Migration(Migration):
    operations = [
        AddField("Order", "status", CharField(max_length=20, null=True)),
    ]

# 0005_backfill_status.py
class Migration(Migration):
    dependencies = [("models", "0004_add_status_nullable")]
    operations = [
        BackfillColumn("Order", "status", value="pending", batch_size=5000),
    ]

# 0006_status_not_null.py
class Migration(Migration):
    dependencies = [("models", "0005_backfill_status")]
    operations = [
        AlterColumnNotNullSafe("Order", "status"),
    ]
```

## Приём 2: переименование колонки через запись в обе {: #renaming-a-column }

Операции `RenameColumnSafe` нет — настоящее переименование без остановки приложения — это рецепт из 4
шагов из существующих операций (`AddField`/`RemoveField`) и `BackfillColumn`, с периодом записи в обе
колонки в коде приложения между ними, который к hare-orm не относится:

1. **Расширение** — добавьте новую колонку; старый код чтения и записи это не затрагивает:

   ```python
   operations = [AddField("Order", "customer_email", CharField(max_length=255, null=True))]
   ```

2. **Заполнение** — заполните новую колонку у существующих строк. `BackfillColumn` подходит, если
   все строки получают одно и то же значение (общее значение по умолчанию или заглушка); если новая
   колонка должна получить *собственное* значение каждой строки из старой, используйте миграцию
   данных `RunPython` с пакетным
   `UPDATE ... SET customer_email = email WHERE customer_email IS NULL` — `BackfillColumn` записывает
   одно значение на всю пачку и не может ссылаться на другую колонку строки.

   ```python
   async def backfill_customer_email(apps, schema_editor):
       while True:
           rowcount, _ = await schema_editor.client.execute(
               'UPDATE "order" SET "customer_email" = "email" '
               'WHERE "id" IN (SELECT "id" FROM "order" WHERE "customer_email" IS NULL LIMIT 5000)'
           )
           if not rowcount:
               break

   operations = [RunPython(backfill_customer_email, RunPython.noop)]
   ```

3. **Запись в обе** — выпустите код приложения, который какое-то время пишет в обе колонки, чтобы и
   старый, и новый код чтения видели актуальные данные (переопределённый `Model.save()` или то же самое
   везде, где пишутся строки):

   ```python
   class Order(Model):
       email = fields.CharField(max_length=255)
       customer_email = fields.CharField(max_length=255, null=True)

       async def save(self, *args, **kwargs):
           self.customer_email = self.email
           await super().save(*args, **kwargs)
   ```

4. **Сжатие** — когда весь код чтения и записи обновлён, а все строки заполнены, удалите старую
   колонку:

   ```python
   operations = [RemoveField("Order", "email")]
   ```

Каждый шаг выпускается и какое-то время работает в продакшене, прежде чем выполняется следующий, —
именно это и даёт работу без остановки, а не какой-то механизм внутри hare-orm.
