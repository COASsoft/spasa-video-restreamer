"""MISB ST 0102 (Security Metadata Local Set) parser for the SPASA W3-T4 integration.

Decodes the nested Security Local Set carried in UAS Datalink (ST 0601) tag 48 into the
flat contract the SPASA server stamps onto a video feed's classification:

    {"classification": "SECRET", "classifyingCountry": "//US", "releasability": ["USA", "ESP"]}

Only the fields SPASA enforces are extracted (classification level + releasability
nations); the rest of ST 0102 is ignored. Pure (no ffmpeg/IO) so it is unit-testable in
isolation, mirroring ``shared/vmti.py`` (W2-T4).
"""

from typing import Optional

# ST 0102 tag 1 — Security Classification enum (MISB ST 0102.12, Table 1).
_CLASSIFICATION = {
    1: "UNCLASSIFIED",
    2: "RESTRICTED",
    3: "CONFIDENTIAL",
    4: "SECRET",
    5: "TOP SECRET",
}

_TAG_CLASSIFICATION = 1
_TAG_CLASSIFYING_COUNTRY = 3
_TAG_RELEASING_INSTRUCTIONS = 6


def _parse_ber_length(data: bytes, offset: int):
    """Returns ``(length, new_offset)`` for a BER-encoded length starting at ``offset``."""
    first = data[offset]
    offset += 1
    if first < 0x80:
        return first, offset
    num_bytes = first & 0x7F
    length = 0
    for _ in range(num_bytes):
        length = (length << 8) | data[offset]
        offset += 1
    return length, offset


def _split_countries(raw: str):
    """Splits an ST 0102 country-code string into individual codes. The field packs
    tri-/digraph codes with separators that vary by source (``//``, ``;``, spaces), so we
    tokenise on any run of non-alphabetic characters."""
    out = []
    token = []
    for ch in raw:
        if ch.isalpha():
            token.append(ch)
        elif token:
            out.append("".join(token))
            token = []
    if token:
        out.append("".join(token))
    return out


def parse_security_ls(raw: Optional[bytes]) -> Optional[dict]:
    """Parses an ST 0102 Security Local Set (the raw bytes of ST 0601 tag 48).

    Returns the flat security contract, or ``None`` when no usable marking is present
    (empty input, parse error before a level is read, or no classification level) — the
    SPASA server then leaves the feed's classification untouched rather than downgrading
    it on a signal glitch.
    """
    if not raw:
        return None
    classification = None
    classifying_country = None
    releasability = []
    offset = 0
    try:
        while offset < len(raw):
            tag = raw[offset]
            offset += 1
            length, offset = _parse_ber_length(raw, offset)
            value = raw[offset:offset + length]
            offset += length
            if tag == _TAG_CLASSIFICATION and len(value) >= 1:
                classification = _CLASSIFICATION.get(value[0])
            elif tag == _TAG_CLASSIFYING_COUNTRY:
                classifying_country = value.decode("ascii", "ignore").strip() or None
            elif tag == _TAG_RELEASING_INSTRUCTIONS:
                releasability = _split_countries(value.decode("ascii", "ignore"))
    except (IndexError, ValueError):
        # Truncated / malformed LS: keep whatever decoded before the break (fail-soft).
        pass
    if classification is None:
        return None
    return {
        "classification": classification,
        "classifyingCountry": classifying_country,
        "releasability": releasability,
    }
