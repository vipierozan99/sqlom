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

- **`@model` dataclass models.** The column descriptors live on the *metaclass*, and
  no checker models a metaclass data descriptor shadowing a class attribute. So
  `AuthorDC.id` is `int` to a checker (the dataclass field) rather than
  `ColumnExpr[int]`, and comparisons against it are unchecked. `ModelMeta` models are
  fully typed; that is the trade for real `dataclasses` interop.
- **Columns off an `Alias` or `Subquery`.** `mgr.id` is `ColumnExpr[Any]`: both
  resolve names from a runtime column map through `__getattr__`, and a checker cannot
  enumerate them. Reach the column off the model when you want the precise type.
- **`sum_` and `avg`.** `Aggregate[Any]`, because Postgres widens `sum(int)` to
  bigint and `avg(int)` to numeric, which arrives as `Decimal`. `count` is `int` and
  `min_`/`max_` keep the column's type.
- **Six or more selected entities.** The row degrades to `tuple[Any, ...]`.
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
