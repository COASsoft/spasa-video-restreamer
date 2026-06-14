"""Unit tests for the MISB ST 0102 (Security Metadata) Local Set parser
(``shared/security.py``).

Pure parser tests — no Flask / ffmpeg / network. Validate the ST 0102 framing contract
the SPASA server (W3-T4) consumes to stamp a feed's classification.
"""

from shared.security import parse_security_ls


def _tlv(tag: int, value: bytes) -> bytes:
    """Encodes one ST 0102 tag with a short-form BER length (test values are < 128 B)."""
    assert len(value) < 0x80
    return bytes([tag, len(value)]) + value


def test_full_marking_round_trip():
    ls = (
        _tlv(1, bytes([4]))            # Security Classification = SECRET
        + _tlv(3, b"//USA")           # Classifying Country
        + _tlv(6, b"USA;ESP")         # Releasing Instructions (REL TO)
    )
    out = parse_security_ls(ls)
    assert out is not None
    assert out["classification"] == "SECRET"
    assert out["classifyingCountry"] == "//USA"
    assert out["releasability"] == ["USA", "ESP"]


def test_level_only():
    out = parse_security_ls(_tlv(1, bytes([1])))  # UNCLASSIFIED
    assert out == {
        "classification": "UNCLASSIFIED",
        "classifyingCountry": None,
        "releasability": [],
    }


def test_all_levels():
    expected = {
        1: "UNCLASSIFIED",
        2: "RESTRICTED",
        3: "CONFIDENTIAL",
        4: "SECRET",
        5: "TOP SECRET",
    }
    for code, name in expected.items():
        out = parse_security_ls(_tlv(1, bytes([code])))
        assert out["classification"] == name


def test_releasability_separators():
    # Mixed separators ('/', spaces, ';') all tokenise to clean codes.
    ls = _tlv(1, bytes([4])) + _tlv(6, b"// USA / GBR ; ESP")
    out = parse_security_ls(ls)
    assert out["releasability"] == ["USA", "GBR", "ESP"]


def test_no_level_returns_none():
    # A marking without a classification level is unusable ⇒ None (never declassify).
    assert parse_security_ls(_tlv(3, b"//USA")) is None


def test_empty_and_none():
    assert parse_security_ls(b"") is None
    assert parse_security_ls(None) is None


def test_unknown_level_code_returns_none():
    # Reserved/unknown enum value ⇒ no recognised level ⇒ None (fail-closed).
    assert parse_security_ls(_tlv(1, bytes([9]))) is None


def test_truncated_ls_is_fail_soft():
    # Level present, then a tag claiming more bytes than remain: keep the level.
    ls = _tlv(1, bytes([4])) + bytes([6, 50]) + b"USA"  # tag 6 says 50 B, only 3 present
    out = parse_security_ls(ls)
    assert out is not None
    assert out["classification"] == "SECRET"
