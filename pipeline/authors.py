"""Author identity — one normalization, used everywhere.

Two places depend on answering "is this the same writer?" and they must agree:
attestation (a repeat from an author already attesting is emphasis, not
corroboration) and the backlog ledger (batches are per author). If they drifted,
corroboration counts and batch boundaries would disagree about who wrote what.
"""
from __future__ import annotations


def author_key(raw: str | None) -> str | None:
    """Normalize a From header to a bare lowercase email.

    `The Author <Author+weekly@Substack.com>` → `author@substack.com`, so display-name
    changes and per-list `+suffix` addressing don't fragment one writer into several.
    Returns None for empty input; returns the input lowercased if it isn't an address.
    """
    raw = (raw or "").strip()
    if not raw:
        return None
    if "<" in raw and ">" in raw:
        raw = raw[raw.find("<") + 1 : raw.find(">")]
    email = raw.strip().lower()
    if "@" in email:
        local, _, domain = email.partition("@")
        email = f"{local.split('+')[0]}@{domain}"
    return email or None
