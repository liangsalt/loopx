"""Discovery contract only; importing the catalog never loads the DV provider."""
from __future__ import annotations

CHIP_VERIFICATION_CATALOG_ENTRY = {
    "id": "chip-verification",
    "origin": "builtin",
    "visibility": "public",
    "provider_id": "loopx-core",
    "title": "Chip design and UVM verification from shared specification contracts",
    "status": "active-preview",
    "default_enabled": False,
    "documentation": {
        "source_root": "loopx/capabilities/chip_verification",
        "site_root": "capabilities/chip-verification",
        "canonical": "README.md",
    },
    "real_world_anchor": "A chip specification may start at requirements or microarchitecture. Design and UVM verification must share module and interface contracts across bounded sessions while retaining real tool evidence and incomplete integration obligations.",
    "user_value": "Persist shared source contracts, RTL/IP and UVM work, supply bounded packets to one selected agent runtime, and resume either track without loading the complete specification or retaining a host conversation.",
    "entry_command": "loopx chip-verification --help",
    "commands": [
        {"command": "loopx chip-verification --workspace . doctor",
         "purpose": "Read local DV and simulator readiness without initializing a workspace.",
         "write_boundary": "read-only"},
        {"command": "loopx chip-verification --workspace . import --spec spec.json --execute",
         "purpose": "Initialize source decomposition and shared design/DV work from any specification view.",
         "write_boundary": "local DV workspace; requires --execute"},
        {"command": "loopx chip-verification --workspace . next",
         "purpose": "Read the next domain work item; host sessions remain independent of its durable state.",
         "write_boundary": "read-only"},
        {"command": "loopx chip-verification --workspace . engineering-status",
         "purpose": "Read shared contract revisions, both engineering tracks and outstanding module integration obligations.",
         "write_boundary": "read-only"},
        {"command": "loopx chip-verification --workspace . install-trae --execute",
         "purpose": "Install workspace-scoped Trae integration for the same capability.",
         "write_boundary": "workspace integration files; requires --execute"},
    ],
    "implemented_protocols": [
        {"schema_version": "chip_verification_admission_v0",
         "module": "loopx.control_plane.capabilities.chip_verification",
         "doc": "loopx/capabilities/chip_verification/README.md"},
    ],
    "smokes": [
        "node --experimental-strip-types --test tests/control_plane_ts/chip_verification.test.ts",
        "python -m pytest tests/capabilities/test_chip_verification_cli.py -q",
    ],
    "next_real_step": "Import a specification, process bounded source and shared-contract tasks, generate design and UVM candidates, and qualify their checks and joint build. Partial module integration and measured coverage remain explicit completion gates.",
}
