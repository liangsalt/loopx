"""Semantic task boundaries are invariant to prose length and tag order."""
import copy

from loopx.capabilities.chip_verification.engine.partitioning import partition_requirements


def memberships(requirements):
    return [{item["id"] for item in group} for group in partition_requirements(requirements)]


def test_semantic_membership_ignores_lengths_and_preserves_cross_module_atom():
    requirements = [
        {"id": "fifo-order", "modules": ["fifo"], "features": ["order"], "interfaces": ["stream"], "statement": "a"},
        {"id": "fifo-order-reset", "modules": ["fifo"], "features": ["order"], "interfaces": ["stream"], "statement": "b"},
        {"id": "fifo-full", "modules": ["fifo"], "features": ["backpressure"], "interfaces": ["stream"], "statement": "c"},
        {"id": "dma-fifo", "modules": ["dma", "fifo"], "features": ["transfer", "order"], "interfaces": ["stream"], "statement": "d"},
    ]
    original = memberships(requirements)
    enlarged = copy.deepcopy(requirements)
    enlarged[0]["statement"] *= 100000
    enlarged[3]["modules"].reverse()
    enlarged[3]["features"].reverse()
    enlarged.reverse()
    assert memberships(enlarged) == original
    assert len(original) == 3
    assert {"dma-fifo"} in original
    assert {"fifo-order", "fifo-order-reset"} in original
    assert sum(len(group) for group in original) == len(requirements)


def test_unlabelled_long_requirements_stay_with_the_owning_scope():
    values = [{"id": "a", "statement": "entire behavior " * 10000}, {"id": "b", "statement": "follow-up"}]
    assert partition_requirements(values) == [values]
