## What this changes

<!-- And why. If it fixes a bug, name the bug. -->

## What you verified

<!--
Not "tests pass" — CI says that. What did you actually check, and on which
backend? A behaviour asserted on only one of sqlite/postgres has not been tested;
that is why the suite parametrises both.
-->

- [ ] `just test . --pg-required` (a skipped postgres half is not a pass)
- [ ] `just lint` and `just typecheck`
- [ ] Types changed? `tests/typing/positive.py` and `negative.py` updated
- [ ] Row path or planner changed? `tests/test_property_hydration.py` still agrees with Core

## Performance

<!--
Delete this section if the change cannot touch the row path.

The gate compares against the merge base and fails over 1.25x. If it fires and
you think the cost is justified, say so here with the numbers. If you are claiming
an improvement, say which contender and on what machine — see
docs/METHODOLOGY.md, and remember absolutes are machine-specific.
-->
