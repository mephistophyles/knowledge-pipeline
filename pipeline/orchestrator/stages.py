"""Stage registry: order, resource class, and which stages a worker handles.

Each stage runs on exactly one resource class so a worker of that class claims
only its stages (plan §3). Audio stages are registered for the future spot-GPU
burst worker (plan §14.1) but no current source type routes through them.

The per-source *chain* determines transitions. For steps 1–2 only the paste /
personal-note chains are exercised; downstream LLM stages are stubs (build
step 5) so the state machine can be hand-walked end-to-end today.
"""
from __future__ import annotations

# stage → resource class
STAGE_RESOURCE: dict[str, str] = {
    "source_note": "cheap",
    "extract_claims": "llm",
    "dedup": "cheap",
    "entities": "llm",
    "personal": "io",
    # future audio chain — handled by the burst GPU worker (build step 4)
    "transcribe": "gpu",
    "diarize": "gpu",
    "speaker_map": "cheap",
}

# source_type → ordered stage chain
CHAINS: dict[str, list[str]] = {
    "default": ["source_note", "extract_claims", "dedup", "entities"],
    "personal_note": ["personal"],
}


def chain_for(source_type: str | None) -> list[str]:
    return CHAINS.get(source_type or "", CHAINS["default"])


def first_stage(source_type: str | None) -> str:
    return chain_for(source_type)[0]


def next_stage(source_type: str | None, stage: str) -> str | None:
    chain = chain_for(source_type)
    i = chain.index(stage)
    return chain[i + 1] if i + 1 < len(chain) else None


def stages_for_resource(resource_class: str) -> list[str]:
    return [s for s, r in STAGE_RESOURCE.items() if r == resource_class]


RESOURCE_CLASSES = sorted(set(STAGE_RESOURCE.values()))
