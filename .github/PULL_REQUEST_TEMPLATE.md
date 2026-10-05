## What & why / Что и зачем

<!-- What does this change, and why is it needed? Link an issue if there is one.
     Что меняется и зачем это нужно? Укажите issue, если он есть. -->

## How was this tested? / Как это проверено?

<!-- New/updated tests are expected for anything beyond a trivial fix - which ones, and what do
they cover? If you only ran things manually, say what you ran.
     Для всего, кроме мелкого исправления, ожидаются новые или обновлённые тесты - какие и что они
проверяют? Если проверяли только вручную, напишите, что запускали. -->

## Checklist / Чек-лист

- [ ] `make check` passes (style + mypy + bandit) / `make check` проходит
- [ ] `make test_sqlite` and `make test_columnar` pass; `make test_postgres_asyncpg` too if this
      touches PostgreSQL / `make test_sqlite` и `make test_columnar` проходят, а если затронут
      PostgreSQL - и `make test_postgres_asyncpg`
- [ ] `make docs` passes if this touches `docs/` / `make docs` проходит, если затронута `docs/`
- [ ] Docs updated under `docs/` (both the `.md` and its `.ru.md` sibling) if this changes public
      behavior / Документация в `docs/` обновлена (и `.md`, и `.ru.md`), если меняется публичное
      поведение
- [ ] A line under `## [Unreleased]` in `CHANGELOG.md` and in `CHANGELOG.ru.md` if this is a
      user-visible change (breaking changes under *Breaking changes*, with what to change in
      calling code) / Строка в `## [Unreleased]` в `CHANGELOG.md` и `CHANGELOG.ru.md`, если
      изменение заметно пользователю (несовместимое - в *Breaking changes*, с тем, что поменять в
      вызывающем коде)
- [ ] Every commit is signed off (`git commit -s`, see CONTRIBUTING.md → Sign-off) / Каждый коммит
      подписан (`git commit -s`, см. CONTRIBUTING.ru.md → Подпись)
- [ ] No unrelated formatting/reordering churn mixed into the diff / В изменениях нет посторонних
      правок форматирования и перестановок
