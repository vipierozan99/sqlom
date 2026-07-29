# Type-level tests

Yes, you can test types in Python, and there are three techniques worth combining.
All three are used here, and none of them runs the code.

### 1. `typing.assert_type` — positive assertions

Stdlib since 3.11. A type checker verifies the inferred type matches *exactly*; at
runtime it returns its first argument and does nothing.

```python
assert_type(Author.id, ColumnExpr[int])     # passes
assert_type(Author.id, ColumnExpr[str])     # checker error
```

Exactness is the point. `assert_type(x, object)` fails even though every value is an
object, so a type that has silently decayed to `Any` is caught — which is the failure
mode that matters most, because `Any` makes everything else pass.

### 2. Expected errors — negative assertions

A checker reporting nothing is not proof that a mistake would be caught. Both
checkers can be told "this line must fail", so the assertion fails if the error ever
stops happening:

```python
Author.id > "abc"   # type: ignore[operator]   # mypy, with --warn-unused-ignores
Author.id > "abc"   # pyright: ignore[reportOperatorIssue]
```

Run mypy with `--warn-unused-ignores` and pyright with
`reportUnnecessaryTypeIgnoreComment`, and an ignore that stops being needed becomes
an error. That is what turns a comment into a test.

### 3. Run the checkers from the test suite

`tests/test_typing.py` shells out to mypy and pyright and asserts they are clean, so
`pytest` fails when the types regress. Both are run because they disagree: pyright
is stricter about descriptor overloads and mypy about variance, and a feature that
only works in one of them is not something to advertise.

## What is deliberately untyped

- **Columns off an `Alias`, `Subquery` or `CTE`.** `mgr.id` is `ColumnExpr[Any]`: all
  three resolve names from a runtime column map through `__getattr__`, and a checker
  cannot enumerate them. Reach the column off the model when you want the precise
  type. One knock-on: `Query(cte.some_column)` has **no** `assert_type` in
  `positive.py`, because the checkers disagree about it. mypy lets `Any` match the
  whole-model overload first and infers `Query[Any]`; pyright picks the
  single-column overload and infers `Query[tuple[Any]]`. Both are defensible
  readings of an `Any` argument, so asserting either would fail the other — it is
  recorded rather than picked.
- **`sum_` and `avg`.** `Aggregate[Any]`, because Postgres widens `sum(int)` to
  bigint and `avg(int)` to numeric, which arrives as `Decimal`. `count` is `int` and
  `min_`/`max_` keep the column's type.
- **Six or more selected entities.** The row degrades to `tuple[Any, ...]`.
- **DML row types.** `fetch_all()` on a statement with `RETURNING` is `list[Any]`.
  `returning()` is chained after construction, so a checker cannot re-parameterise
  the statement the way `Query`'s constructor overloads can. The DML builders are
  therefore not generic at all, rather than carrying a type variable that resolves
  to `Never`. The same applies to `on_conflict_do_update(set_=...)`: `set_` is
  `dict[str, Any]`, so a value of the wrong type for its column is not caught. The
  keys are runtime-checked against the model's columns instead.
- **`-column` on a non-numeric column.** `__neg__` takes no operand, so there is no
  argument to constrain by the column's type. Expressing "numeric columns only"
  would need per-type descriptor classes instead of one generic `ColumnExpr`.
- **Outer-join nullability.** `Query(User, Post).outer_join(...)` is typed
  `tuple[User, Post]`, but at runtime the second slot is `Post | None`. The join is
  declared after the row type is fixed, so expressing this would need the builder to
  re-type itself per call. Left as a known gap rather than papered over.

## Running

```bash
pip install mypy pyright
python3 -m pytest tests/test_typing.py       # both checkers, via pytest
python3 -m mypy --config-file pyproject.toml tests/typing/
pyright tests/typing/
```
