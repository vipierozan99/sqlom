lint *args='':
    uv run ruff check . {{ args }}

typecheck *args='':
    uv run basedpyright {{ args }}

test regex="." *args='':
    #!/usr/bin/env bash
    set -euo pipefail

    FORCE_COLOR=1 uv run pytest ./tests -k {{ regex }}  {{ args }}

# Coverage over `rowform` (config in pyproject). CI runs this with
# --cov-fail-under, so a drop fails the build rather than being noticed later.
cov *args='':
    uv run pytest ./tests --cov --cov-report=term-missing {{ args }}

# Unified benchmark CLI. `just bench --help` lists every subcommand.
bench *args='':
    uv run --all-groups python -m benchmarks {{ args }}