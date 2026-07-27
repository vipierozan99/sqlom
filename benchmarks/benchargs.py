"""Shared argument validation for the benchmark harnesses.

Non-positive knobs do not fail cleanly, they fail *late* and in ways that read as
bugs somewhere else:

    --concurrency 0   ->  asyncio.gather() of nothing, then `cpu / total`
                          with total == 0  ->  ZeroDivisionError
    --duration 0      ->  every worker exits before its first request, same
    --repeat 0        ->  statistics.median([])  ->  StatisticsError
    --rows 0          ->  an empty table, so every contender "agrees" on `[]`
                          and the equivalence gate passes vacuously

That last one is the reason this is worth a shared module rather than a comment:
an empty result set makes the fairness gate pass while measuring nothing, which
is the failure mode hardest to notice in the output.

Every harness calls `validate(parser, args)` immediately after `parse_args()`, so
the error arrives from argparse with a usage message instead of a traceback part
way through a run.
"""

# Values where zero is meaningless and negative is nonsense.
_POSITIVE = ("concurrency", "duration", "repeat", "pool_size", "iterations",
             "number", "rows", "trials", "timeout")
# Zero is a legitimate choice: no warmup, or a zero-row LIMIT.
_NON_NEGATIVE = ("warmup", "limit")


def _each(value):
    """Yield the numbers in a knob that may be a scalar or a "1,8,32" sweep."""
    if isinstance(value, str):
        for part in value.split(","):
            part = part.strip()
            if part:
                yield part, float(part)
    else:
        yield value, float(value)


def validate(parser, args, extra_positive=(), extra_non_negative=()):
    """Reject non-positive benchmark knobs via `parser.error`.

    Only attributes the parser actually defined are checked, so harnesses can
    share this without declaring every knob.
    """
    checks = [(name, True) for name in (*_POSITIVE, *extra_positive)]
    checks += [(name, False) for name in (*_NON_NEGATIVE, *extra_non_negative)]

    for name, must_be_positive in checks:
        value = getattr(args, name, None)
        if value is None:
            continue
        flag = "--" + name.replace("_", "-")
        try:
            for shown, number in _each(value):
                if must_be_positive and number <= 0:
                    parser.error(f"{flag} must be > 0, got {shown}")
                if not must_be_positive and number < 0:
                    parser.error(f"{flag} must be >= 0, got {shown}")
        except ValueError:
            parser.error(f"{flag} must be a number (or a comma-separated list "
                         f"of them), got {value!r}")
    return args
