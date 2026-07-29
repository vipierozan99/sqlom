from collections.abc import Callable
from typing import Any


def compile_source(source: str, function_name: str) -> Callable:
    """Compile a source string into a callable function."""
    namespace: dict[str, Any] = {}
    exec(source, namespace)  # noqa: S102 -- compiling our own generated source, not external input
    return namespace[function_name]
