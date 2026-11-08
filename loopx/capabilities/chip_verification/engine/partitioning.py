"""Engineering work boundaries from explicit semantics, never prose length.

Labels are supplied by source interpretation. This grouping neither invents
module aliases nor infers relationships from words appearing near each other.
"""
from __future__ import annotations

import json


def requirement_scope_key(requirement):
    """Keep a cross-module requirement and its complete feature set atomic."""
    return {name: sorted({value for value in requirement.get(name, [])
                          if isinstance(value, str) and value.strip()})
            for name in ("modules", "features", "interfaces")}


def partition_requirements(requirements):
    """Return deterministic complete requirement groups by declared identity.

    An unlabelled requirement remains in the owning scope. A long requirement
    is not cut, and changing its wording without changing labels cannot create
    extra work units. Stage and artifact ownership are defined by the caller.
    """
    grouped = {}
    for requirement in requirements:
        key = json.dumps(requirement_scope_key(requirement), sort_keys=True, ensure_ascii=False)
        grouped.setdefault(key, []).append(requirement)
    return [sorted(grouped[key], key=lambda item: str(item.get("id", item.get("local_id", ""))))
            for key in sorted(grouped)]
