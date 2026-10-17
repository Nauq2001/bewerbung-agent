"""Source discovery.

Public sources are imported normally. Personal sources are discovered at
runtime from ``app/sources/personal/``, a directory this repository does not
ship: the package is public, the scrapers that have no permission to be
public are not.

A missing personal directory is the normal case for anyone who clones this
repo, so discovery must be silent about it, not warn.
"""

from __future__ import annotations

import importlib
import importlib.util
import pkgutil
from pathlib import Path

from app.sources.base import JobSource, SourceTier

_PERSONAL_PKG = "app.sources.personal"


def _public_sources() -> list[JobSource]:
    from app.sources.public.arbeitnow import ArbeitnowSource

    return [ArbeitnowSource()]


def _personal_sources() -> list[JobSource]:
    """Load every ``SOURCE`` exported by a module under app/sources/personal/.

    Convention: each module defines a module-level ``SOURCE`` instance. Keeps
    discovery from having to guess class names, and lets a plugin decide for
    itself how it is constructed (a CLI wrapper needs a path, an API client
    needs a key).
    """
    spec = importlib.util.find_spec(_PERSONAL_PKG)
    if spec is None or not spec.submodule_search_locations:
        return []

    found: list[JobSource] = []
    for location in spec.submodule_search_locations:
        for module_info in pkgutil.iter_modules([str(Path(location))]):
            if module_info.name.startswith("_"):
                continue
            module = importlib.import_module(f"{_PERSONAL_PKG}.{module_info.name}")
            source = getattr(module, "SOURCE", None)
            if source is None:
                continue
            if getattr(source, "tier", None) is not SourceTier.PERSONAL:
                raise ValueError(
                    f"{module_info.name}: a source under personal/ must declare "
                    f"tier=SourceTier.PERSONAL"
                )
            found.append(source)
    return found


def all_sources() -> list[JobSource]:
    """Every installed source, regardless of who may run it.

    Callers must pass this through ``enabled_sources`` before running anything.
    """
    return [*_public_sources(), *_personal_sources()]
