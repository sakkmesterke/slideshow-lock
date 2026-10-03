"""Byte strings for the file checks: they have the marker structure of a JPEG, no more."""

from __future__ import annotations


def fake_jpeg(entropy_bytes: int = 3000) -> bytes:
    """A byte string with the marker structure of a JPEG (not decodable, enough for the checks):
    SOI, APP0, an APP1 that carries a thumbnail with its own end-of-image marker, DQT, SOF0,
    SOS, entropy-coded data with stuffed FF00 bytes and restart markers, EOI."""
    soi = b"\xff\xd8"
    app0 = (
        b"\xff\xe0"
        + (16).to_bytes(2, "big")
        + b"JFIF\x00"
        + b"\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    )
    thumb = b"\xff\xd8\xff\xdb\x00\x04\x00\x00\xff\xd9"  # a whole tiny "JPEG" inside APP1
    app1 = b"\xff\xe1" + (len(thumb) + 2).to_bytes(2, "big") + thumb
    dqt = b"\xff\xdb" + (67).to_bytes(2, "big") + b"\x00" + bytes(64)
    sof = b"\xff\xc0" + (11).to_bytes(2, "big") + b"\x08\x00\x10\x00\x10\x01\x01\x11\x00"
    sos = b"\xff\xda" + (8).to_bytes(2, "big") + b"\x01\x01\x00\x00\x3f\x00"
    entropy = bytearray()
    for i in range(entropy_bytes):
        entropy += (
            b"\xff\x00"
            if i % 97 == 0
            else bytes([(i * 7) % 251 + 1 if (i * 7) % 251 != 255 else 1])
        )
        if i % 500 == 499:
            entropy += bytes([0xFF, 0xD0 + (i // 500) % 8])  # restart marker
    return soi + app0 + app1 + dqt + sof + sos + bytes(entropy) + b"\xff\xd9"
