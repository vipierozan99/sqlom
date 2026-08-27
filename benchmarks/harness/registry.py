"""Contender registry — every contender defined exactly once.

The old suite defined `Query(User).where(...)` ~10 times and the SA Core
`.mappings()` idiom ~7 times, one of which carried a verbatim-duplicated 8-line
docstring. `@contender` registers a factory once; `bench micro` and the
FastAPI worker (`service/app.py`) both read from the same `REGISTRY`, so a fix
or a new contender is written in one place and is visible to both.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, NamedTuple


class ContenderInit(NamedTuple):
    """The one argument every contender factory takes, regardless of backend
    — `handle` is a sqlite db path (`str`) for `backend="sqlite"` contenders
    or precomputed rows (`list[tuple[Any, ...]]`) for `backend="mock"` ones.
    One shape for every factory, rather than sqlite contenders taking
    `(path, limit)` and mock ones taking `(rows, limit)`, is what makes
    `Callable[[ContenderInit], ...]` a single type the decorator can check
    instead of `Callable[..., Any]`."""

    handle: Any
    limit: int


Target = Callable[[], Awaitable[bytes]]
Teardown = Callable[[], Awaitable[None]]
ContenderFactory = Callable[[ContenderInit], Awaitable[tuple[Target, Teardown]]]

# Keyed by slug (see `_kebab`/`ContenderSpec.slug`), which is unique by
# construction — {backend}-{shape}-{kebab(name)} — so a flat-shape and a
# join-shape contender can still legitimately share a display name without
# colliding, and so can two backends of the same shape.
REGISTRY: dict[str, ContenderSpec] = {}

# Shared verbatim across every CLI command's --only/--shape/--case option so a
# reader never has to guess where valid values come from — `bench contenders
# list` is also the machine-readable (`--json`) way to get this same
# information from a script instead of a `--help` string.
ONLY_HELP = "substring match on contender name — see `bench contenders list`"
SHAPE_HELP = "see `bench contenders shapes`"
CASE_HELP = "a contender slug, '{backend}-{shape}-{name}' — see `bench contenders list`"


def _kebab(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


@dataclass(frozen=True, slots=True)
class ContenderSpec:
    """One entry per contender. `factory` takes one `ContenderInit` and
    returns `(target, teardown)` — `target()` runs one unit of work and
    returns response-ready bytes (for the equivalence gate); `teardown()`
    releases whatever the factory opened (pool, engine, connection)."""

    name: str
    slug: str  # unique kebab-case id: "{backend}-{shape}-{kebab(name)}"
    description: str
    # "sqlite" | "postgres" (asyncpg) | "postgres-psycopg" | "mock" | "none".
    # One group per driver where two drivers exist, because the equivalence gate
    # and the `vs rowform` ratio are both per group — see contenders.py's psycopg
    # section.
    backend: str
    shape: str  # "flat" | "join" | "n/a"
    shipped: bool  # False for a floor/baseline that ships nothing (e.g. raw asyncpg)
    factory: ContenderFactory
    tags: tuple[str, ...] = field(default_factory=tuple)


def contender(
    name: str, *, backend: str, shape: str, description: str, shipped: bool = True,
    tags: tuple[str, ...] = (),
) -> Callable[[ContenderFactory], ContenderFactory]:
    """Register an async contender factory under a slug derived from
    `name`/`backend`/`shape` — see `ContenderSpec.slug`.

    Typed as `ContenderFactory -> ContenderFactory` (the decorator returns
    the function unchanged) rather than `Callable[..., Any]`, so a factory
    with the wrong shape — wrong `init` type, or not returning
    `tuple[Target, Teardown]` — is a type error at the `@contender(...)` site,
    not a runtime surprise the first time something calls it.

    Re-registering the same slug is a mistake, not a redefinition — it means
    two files think they own the same contender — so it raises rather than
    silently letting the second one win. `description` is required (not
    defaulted to the factory's docstring): a docstring is for whoever reads
    the source, `description` is for `bench contenders list`, which is meant
    to stand on its own without anyone opening `contenders.py`.
    """
    slug = f"{backend}-{shape}-{_kebab(name)}"

    def decorator(factory: ContenderFactory) -> ContenderFactory:
        if slug in REGISTRY:
            raise ValueError(
                f"contender slug {slug!r} is already registered "
                f"(name={REGISTRY[slug].name!r})"
            )
        REGISTRY[slug] = ContenderSpec(
            name=name, slug=slug, description=description, backend=backend, shape=shape,
            shipped=shipped, factory=factory, tags=tags,
        )
        return factory

    return decorator


def get(slug: str) -> ContenderSpec:
    try:
        return REGISTRY[slug]
    except KeyError:
        known = ", ".join(sorted(REGISTRY))
        raise KeyError(f"no contender with slug {slug!r}; known: {known}") from None


def select(*, backend: str | None = None, shape: str | None = None,
           tags: tuple[str, ...] = (), only: str | None = None) -> list[ContenderSpec]:
    """Filter the registry. `only` matches a case-insensitive substring of the
    name — the same "isolate one contender" knob every old script had as
    `--only`."""
    specs = REGISTRY.values()
    if backend is not None:
        specs = (s for s in specs if s.backend == backend)
    if shape is not None:
        specs = (s for s in specs if s.shape == shape)
    if tags:
        specs = (s for s in specs if set(tags) <= set(s.tags))
    if only is not None:
        needle = only.lower()
        specs = (s for s in specs if needle in s.name.lower())
    return list(specs)
