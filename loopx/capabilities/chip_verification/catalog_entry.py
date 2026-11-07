"""Discovery contract only; importing the catalog never loads the DV provider."""
from __future__ import annotations

CHIP_VERIFICATION_CATALOG_ENTRY = {
    "id": "chip-verification",
    "origin": "builtin",
    "visibility": "public",
    "provider_id": "loopx-core",
    "title": "Chip verification from bounded specification work to UVM evidence",
    "status": "active-preview",
    "default_enabled": False,
    "documentation": {
        "source_root": "loopx/capabilities/chip_verification",
        "site_root": "capabilities/chip-verification",
        "canonical": "README.md",
    },
    "real_world_anchor": "A large chip specification must become a traceable test plan and UVM environment across multiple agent sessions, with simulator evidence kept distinct from generated claims.",
    "user_value": "Persist DV domain state, supply bounded work packets to the user's agent, and recover verification work without loading the complete specification or retaining a host conversation.",
    "entry_command": "loopx chip-verification --help",
    "commands": [
        {"command": "loopx chip-verification --workspace . doctor",
         "purpose": "Read local DV and simulator readiness without initializing a workspace.",
         "write_boundary": "read-only"},
        {"command": "loopx chip-verification --workspace . import --spec spec.json --execute",
         "purpose": "Explicitly initialize specification decomposition and durable DV work.",
         "write_boundary": "local DV workspace; requires --execute"},
        {"command": "loopx chip-verification --workspace . next",
         "purpose": "Read the next domain work item; host sessions remain independent of its durable state.",
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
    "next_real_step": "Inspect doctor, explicitly import a specification, claim a bounded work packet in the host, and submit evidence before reporting verification coverage.",
}
