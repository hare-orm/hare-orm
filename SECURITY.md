# Security Policy

[Русская версия](SECURITY.ru.md)

## Supported versions

hare-orm is before 1.0 and has one line of development. Security fixes go into the latest minor
release as a new patch release. Older minor releases get no backports.

| Version | Supported |
| --- | --- |
| The latest minor release (`0.x`, its newest patch) | Yes |
| Any older minor release | No - upgrade to the latest one |
| `dev` between releases | Fixed there first, then released |

## Reporting a vulnerability

**Please don't open a public issue for a security vulnerability.**

Preferred: use GitHub's private vulnerability reporting for this repository —
[Security → Report a vulnerability](https://github.com/hare-orm/hare-orm/security/advisories/new).

If that's not an option, email **[vlad.yaremenko.98@yandex.ru](mailto:vlad.yaremenko.98@yandex.ru)** with:

- A description of the vulnerability and its potential impact.
- Steps to reproduce it (a minimal repro model/query is ideal).
- The hare-orm version or commit.
- Which dialect and driver it affects (SQLite, PostgreSQL via `asyncpg` or via the Rust `rust_pg`
  driver, or any dialect) — hare-orm's own query builder, and its vendored copy of PyPika, are both
  in scope; so are the Rust extensions under `rust/`.

## What happens next

1. You get an acknowledgment within three working days.
2. A maintainer confirms the issue, assesses its severity and keeps you informed of progress in the
   private advisory.
3. The fix is prepared in the private advisory's fork and released as a patch release. The
   advisory is published at the same time, with a CVE requested through GitHub when the issue
   warrants one, and the release is listed under *Security* in [CHANGELOG.md](CHANGELOG.md).
4. We agree the disclosure date with you. By default it is the day of the fixed release, and at
   the latest 90 days after your report.

You are credited in the advisory and in the release notes, unless you'd rather stay anonymous.

## Scope

In scope: hare-orm itself (`hare/`, including its dialects and `hare.contrib`), its Rust
extensions (`rust/`: the PostgreSQL driver and the row hydration accelerator), and the
migration/CLI tooling it ships. Vulnerabilities in a database engine you point hare-orm at
(PostgreSQL, SQLite), in a third-party dialect package, or in an application built with
hare-orm, are out of scope for this repository — report those to the relevant upstream project
or your own application's maintainers.
