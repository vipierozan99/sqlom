lint *args='':
    uv run ruff check . {{ args }}

typecheck *args='':
    uv run basedpyright {{ args }}

test regex="." *args='':
    #!/usr/bin/env bash
    set -euo pipefail

    FORCE_COLOR=1 uv run pytest ./tests -k {{ regex }}  {{ args }}