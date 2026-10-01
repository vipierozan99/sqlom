# Security

## Reporting

Use GitHub's private vulnerability reporting (**Security → Report a
vulnerability** on <https://github.com/vipierozan99/sqlom>), not a public issue.
Include the statement or declaration involved, the driver, and the Python,
SQLAlchemy and driver versions. Only `0.1.x` exists; fixes land on `main`.

## What the codegen reaches

`rowform/compile.py` builds one hydrator per statement shape as Python source
and `exec`s it. Everything interpolated into that source comes from your own
model declarations: attribute names are `Mapped[]` field names, which are valid
Python identifiers by construction; model classes and result processors enter
the namespace as objects, not text. Row values and bind parameters are never
interpolated — they arrive as call arguments. If you build model classes from
untrusted input you are choosing what goes into generated code, as with
`dataclasses.make_dataclass`. The source is on `hydrate.__source__`.

## SQL

rowform generates no SQL. Statements are compiled by SQLAlchemy Core and run as
parameterised queries with bound values. `copy_in` quotes table and column
identifiers through the dialect's preparer. `Connection.exec_driver_sql()` sends
a raw string uncompiled — do not build one from untrusted input.
