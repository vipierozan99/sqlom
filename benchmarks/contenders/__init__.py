"""Contender definitions, one per name, registered via
`benchmarks.harness.registry.contender`. Imported (for their registration
side-effects) by every CLI command that needs the registry populated —
`bench micro`, `bench service`, `bench profile`.
"""

from benchmarks.contenders import flat, join

__all__ = ["flat", "join"]
