"""Pinned exercise/reference files are data, not LoopX's Python test suite.

Collecting upstream Cocotb files would require the very native build environment
these standalone Cadence exercises do not depend on, and pollute pinned inputs
with bytecode. Corpus integrity is exercised by the local test_*.py modules.
"""

collect_ignore = ["cases", "issue_cases"]
