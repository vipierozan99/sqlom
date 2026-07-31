# Security

## Supported versions

| Version | Supported |
|---|---|
| 0.1.x | yes — the only line, and pre-release |

The project is early: not on PyPI, and never run in production. There is no
backport branch, so a fix lands on `main`.

## Reporting a vulnerability

Use GitHub's private vulnerability reporting: **Security → Report a
vulnerability** on <https://github.com/vipierozan99/sqlom>. That keeps the report
private until there is something to release. Please do not open a public issue
for anything exploitable.

Include what you'd want to receive: the statement or declaration involved, the
driver (`aiosqlite`, `asyncpg`, `psycopg`), and the versions of
Python, SQLAlchemy and the driver.

## Two things worth knowing about this library

### It generates and `exec`s Python at runtime

`rowform/compile.py` builds a hydrator function per statement shape as source text
and `exec`s it. That is the core of the design, so it deserves a plain statement of
its boundary.

Everything interpolated into that source comes from **your own model declarations,
never from a query result or a request**:

* attribute names come from `Mapped[]` field names, which Python has already
  validated as identifiers,
* model classes and each column's `result_processor` are inserted into the
  namespace as objects, not as text,
* row *values* are never interpolated — they arrive as arguments to the generated
  function at call time, exactly as they would through `Row`.

So a column's contents cannot reach the generated source, and neither can anything
a caller passes as a bind parameter. What *is* in scope: if you build model classes
dynamically from untrusted input — a field name taken from an HTTP request, say —
you are choosing what goes into generated code, and the same caution applies as
with `type()` or `dataclasses.make_dataclass`.

The generated source is on `hydrate.__source__` and logged at DEBUG, so you can
always read what was built.

### SQL is compiled by SQLAlchemy, not by this library

rowform generates no SQL. Statements are compiled by SQLAlchemy Core and executed
as parameterised queries, with values bound through the driver — so the usual
guidance applies unchanged: keep user input in bind parameters and out of
`sa.text()` fragments and identifiers you interpolate yourself.

One rowform-specific note: `Transaction.execute()` also accepts a raw SQL string,
for the DDL and session state a statement object cannot express. That path has no
parameters and no escaping. Do not build those strings out of untrusted input.
