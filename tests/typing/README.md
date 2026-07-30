# Type-level tests

Yes, you can test types in Python, and there are three techniques worth
combining. All three are used here, and none of them runs the code.

### 1. `typing.assert_type` — positive assertions

Stdlib since 3.11. A type checker verifies the inferred type matches *exactly*;
at runtime it returns its first argument and does nothing.

```python
assert_type(author.id, int)     # passes
assert_type(author.id, str)     # checker error
```

Exactness is the point. `assert_type(x, object)` fails even though every value is
an object, so a type that has silently decayed to `Any` is caught — the failure
mode that matters most, because `Any` makes everything else pass.

This is also the file that decided the library's shape. `@sa_model(metadata)`
would be a decorator *factory*, and factories lose field typing entirely: every
`assert_type` in `positive.py` would fail with `Any`. That is why declaration is
a base class with a `dataclass_transform`-decorated metaclass
(docs/PLAN_CORE_COMPILER.md §5b).

### 2. Expected errors — negative assertions

A checker reporting nothing is not proof that a mistake would be caught. It can
be told "this line must fail", so the assertion fails if the error ever stops
happening:

```python
Author(id="one", ...)   # pyright: ignore[reportArgumentType]
```

`pyproject.toml` sets `reportUnnecessaryTypeIgnoreComment = "error"`, so an
ignore that stops being needed becomes an error. That is what turns a comment
into a test.

### 3. Run the checker from the test suite

`tests/test_typing.py` shells out to basedpyright and asserts it is clean, so
`pytest` fails when the types regress.

## What is deliberately imprecise

- **`Model.id` is declared `InstrumentedAttribute[int]` but is an `sa.Column` at
  runtime.** Harmless — both are `ColumnOperators`, which is why `Model.id > 100`
  works — but it is an inherited fiction from `Mapped.__get__`'s overloads.
- **The generated `__init__` accepts a SQL expression where a value is meant.**
  The parameter type is `SQLCoreOperations[int] | int`, inherited from
  `Mapped.__set__`. Not worth fighting.
- **Five or more selected entities.** `fetch_all` degrades to `list[Any]`. The
  overloads are written per arity because that is exactly what a checker knows —
  `Select` is parameterised by a tuple of its selected types — and they stop at
  four rather than growing without end.
- **Writes.** `execute()` returns `Any`: an int rowcount on sqlite and psycopg, a
  status string on asyncpg. Normalising it would hide the difference between "0
  rows matched" and "the statement did nothing".
- **Outer-join nullability.** `select(Author, Book).outerjoin(...)` types as
  `list[tuple[Author, Book]]`, but the second slot is `Book | None` at runtime.
  The join is declared after the row type is fixed, so expressing this would need
  the statement to re-type itself per call. A known gap rather than papered over.
- **`frozen=True` must be declared on the Base too.** A checker treats every model
  under a Base as sharing its dataclass configuration and refuses a frozen class
  inheriting from a non-frozen one — the same rule stdlib dataclasses apply.

## Running

```bash
just typecheck                  # the whole project
just test typing                # via pytest
uv run basedpyright tests/typing/
```
