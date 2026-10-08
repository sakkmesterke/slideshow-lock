"""Take the metadata out of a JPEG without touching the picture: no decoding, no re-encoding.

stdlib only. The file is read as a list of segments and written again with the segments of a
white list; the entropy-coded data of the scan is copied as it is, byte for byte. What stays:

* SOI and EOI, the quantisation tables (DQT), the Huffman tables (DHT), the one baseline frame
  header (SOF0), the restart interval (DRI) and the one scan (SOS and its data): the picture.
* The JFIF header (APP0, at most one) and the Adobe colour marker (APP14, at most one, 14 bytes,
  transform 0-2): they say how to read the picture, they carry no personal data.

Everything else is dropped: Exif (with its thumbnail), XMP, the Photoshop block (APP13), comments
(COM), every ICC profile (APP2, ``ICC_PROFILE``: the bytes of a colour profile belong to whoever
made the profile, not to the author of the picture, so none is kept and none is put back; a picture
without a profile is taken for sRGB) and every other APPn. Two segments are then put back, and
only these two: the author and the licence of the pictures, as one Exif APP1 (``exif_segment``:
Artist and Copyright, two tags, no other IFD) and one XMP APP1 (``xmp_segment``: dc:creator,
dc:rights, xmpRights:WebStatement). Both are built from the constants ``AUTHOR`` and
``LICENSE_NAME`` / ``LICENSE_URL`` that the credit line of ``data/pictures/CREDITS.txt`` is built
from, by the functions that the audit compares with, byte for byte: there is one source for the
text and no second place that writes Exif.

``audit`` is the same white list as a check: it fails on anything that is not on it (an Exif or XMP
segment that is not byte for byte the expected one included, and one that is missing), and on every
oddity of the structure (an unknown marker, a length that runs over the end, no EOI, data after the
EOI), so a file that this parser does not understand is refused, not passed. ``tests/
test_pictures_clean.py`` runs it over ``data/pictures``.

    python3 tools/strip_jpeg_metadata.py --out CLEAN_DIR SOURCE.jpg ...

writes ``CLEAN_DIR/<name>`` for each source and never overwrites a file.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import struct
import sys
from typing import Dict, List, NamedTuple, Optional, Sequence, Tuple
from xml.sax.saxutils import escape

SOI, EOI = 0xD8, 0xD9
SOF0, DHT, DQT, DRI, SOS = 0xC0, 0xC4, 0xDB, 0xDD, 0xDA
APP0, APP1, APP2, APP13, APP14, COM = 0xE0, 0xE1, 0xE2, 0xED, 0xEE, 0xFE
RST = range(0xD0, 0xD8)

#: The one source of the texts: the author, the licence, and what is built from them. The credit
#: line is the text of ``data/pictures/CREDITS.txt``; the rights line is in Exif Copyright and in
#: XMP dc:rights; the author is in Exif Artist and in XMP dc:creator.
AUTHOR = "sakkmesterke (Alexovics Attila)"
LICENSE_NAME = "CC BY-SA 4.0"
LICENSE_URL = "https://creativecommons.org/licenses/by-sa/4.0/"
RIGHTS = f"{LICENSE_NAME}, {AUTHOR}"
CREDITS_LINE = f"Fraktálképek: {AUTHOR}, {LICENSE_NAME}\n"

EXIF_ARTIST, EXIF_COPYRIGHT = 0x013B, 0x8298
#: The two IFD0 entries of the Exif segment, in the order of the tag numbers (a TIFF rule).
EXIF_TAGS: Tuple[Tuple[int, str], ...] = ((EXIF_ARTIST, AUTHOR), (EXIF_COPYRIGHT, RIGHTS))
EXIF_ID = b"Exif\x00\x00"
XMP_ID = b"http://ns.adobe.com/xap/1.0/\x00"
ICC_ID = b"ICC_PROFILE\x00"

#: Words that must not be in any segment that is not entropy-coded data (a second net under the
#: white list; the entropy data is random bytes and is not searched). The Exif and the XMP segment
#: are not searched: they are compared with the expected bytes as a whole.
FORBIDDEN_WORDS = (b"Photoshop", b"Photopea", b"/home/", b"GPS", b"Exif", b"xmpmeta")


class JpegError(ValueError):
    """The file is not a JPEG this module understands, or it is not clean."""


class Segment(NamedTuple):
    marker: int
    data: bytes  # the whole segment: FF xx, the length and the payload (for SOS: the header only)
    entropy: bytes = b""  # SOS only: the entropy-coded data that follows the header


def _app1(payload: bytes) -> bytes:
    return b"\xff" + bytes([APP1]) + (len(payload) + 2).to_bytes(2, "big") + payload


def exif_segment(tags: Sequence[Tuple[int, str]] = EXIF_TAGS) -> bytes:
    """The Exif APP1 segment: a little-endian TIFF with one IFD0 that has the *tags* (ASCII, NUL
    ended, the tag numbers ascending) and no next IFD, no Exif IFD, no GPS IFD, no thumbnail."""
    tiff = b"II" + struct.pack("<HI", 42, 8)
    values = b""
    entries = b""
    first_value = 8 + 2 + 12 * len(tags) + 4
    for tag, text in tags:
        raw = text.encode("ascii") + b"\x00"
        offset = first_value + len(values)
        entries += struct.pack("<HHII", tag, 2, len(raw), offset)
        values += raw + (b"\x00" if len(raw) % 2 else b"")  # a value starts on a word boundary
    tiff += struct.pack("<H", len(tags)) + entries + struct.pack("<I", 0) + values
    return _app1(EXIF_ID + tiff)


def xmp_segment(author: str = AUTHOR, rights: str = RIGHTS, url: str = LICENSE_URL) -> bytes:
    """The XMP APP1 segment: one packet with dc:creator, dc:rights and xmpRights:WebStatement."""
    packet = (
        '<?xpacket begin="\ufeff" id="W5M0MpCehiHzreSzNTczkc9d"?>'
        '<x:xmpmeta xmlns:x="adobe:ns:meta/">'
        '<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
        '<rdf:Description rdf:about="" xmlns:dc="http://purl.org/dc/elements/1.1/"'
        ' xmlns:xmpRights="http://ns.adobe.com/xap/1.0/rights/">'
        f"<dc:creator><rdf:Seq><rdf:li>{escape(author)}</rdf:li></rdf:Seq></dc:creator>"
        f'<dc:rights><rdf:Alt><rdf:li xml:lang="x-default">{escape(rights)}</rdf:li></rdf:Alt>'
        "</dc:rights>"
        f"<xmpRights:WebStatement>{escape(url)}</xmpRights:WebStatement>"
        "</rdf:Description></rdf:RDF></x:xmpmeta>"
        '<?xpacket end="w"?>'
    )
    return _app1(XMP_ID + packet.encode("utf-8"))


def parse(data: bytes) -> List[Segment]:
    """The segments of *data* in file order, SOI and EOI included. Strict: anything that is not a
    well-formed marker structure ends in ``JpegError``."""
    if data[:2] != b"\xff\xd8":
        raise JpegError("no SOI at the start")
    segments = [Segment(SOI, data[:2])]
    pos = 2
    while True:
        if pos + 2 > len(data):
            raise JpegError("no EOI: the file ends inside the marker structure")
        if data[pos] != 0xFF:
            raise JpegError(f"no marker at offset {pos}")
        marker = data[pos + 1]
        if marker == EOI:
            segments.append(Segment(EOI, data[pos : pos + 2]))
            if pos + 2 != len(data):
                raise JpegError(f"{len(data) - pos - 2} bytes after the EOI")
            return segments
        if marker in (0x00, 0xFF, SOI, 0x01, *RST) or 0x02 <= marker <= 0xBF:
            raise JpegError(f"marker FF{marker:02X} at offset {pos} where a segment is expected")
        if pos + 4 > len(data):
            raise JpegError("a segment header runs over the end")
        length = int.from_bytes(data[pos + 2 : pos + 4], "big")
        end = pos + 2 + length
        if length < 2 or end > len(data):
            raise JpegError(f"the segment FF{marker:02X} at offset {pos} has the length {length}")
        if marker != SOS:
            segments.append(Segment(marker, data[pos:end]))
            pos = end
            continue
        scan_end = end
        while True:  # entropy-coded data: FF00 and the restart markers belong to it
            hit = data.find(b"\xff", scan_end)
            if hit < 0 or hit + 1 >= len(data):
                raise JpegError("no EOI: the scan runs to the end of the file")
            if data[hit + 1] == 0x00 or data[hit + 1] in RST:
                scan_end = hit + 2
                continue
            scan_end = hit
            break
        segments.append(Segment(SOS, data[pos:end], data[end:scan_end]))
        pos = scan_end


def _is_jfif(segment: Segment) -> bool:
    return segment.marker == APP0 and segment.data[4:9] == b"JFIF\x00"


def _is_adobe(segment: Segment) -> bool:
    return segment.marker == APP14 and segment.data[4:9] == b"Adobe"


def _is_exif(segment: Segment) -> bool:
    return segment.marker == APP1 and segment.data[4:10] == EXIF_ID


def _is_xmp(segment: Segment) -> bool:
    return segment.marker == APP1 and segment.data[4 : 4 + len(XMP_ID)] == XMP_ID


def _is_icc(segment: Segment) -> bool:
    """Any chunk of an ICC profile (APP2 with the ``ICC_PROFILE`` id), whatever is in it."""
    return segment.marker == APP2 and segment.data[4:16] == ICC_ID


def _keep(segment: Segment) -> bool:
    if segment.marker in (SOI, EOI, DQT, DHT, SOF0, DRI, SOS):
        return True
    return _is_jfif(segment) or _is_adobe(segment)


def _write(segments: List[Segment]) -> bytes:
    return b"".join(segment.data + segment.entropy for segment in segments)


def clean(data: bytes) -> bytes:
    """*data* without the metadata, but for the Exif and XMP segment of the author and the licence.
    Raises ``JpegError`` if the file is not understood or if what is
    left is not clean (``audit``): there is no output that has not passed the check."""
    kept = [segment for segment in parse(data) if _keep(segment)]
    # the author and the licence, after SOI and the JFIF header (the place the dropped ones had)
    at = 2 if len(kept) > 1 and _is_jfif(kept[1]) else 1
    kept[at:at] = [Segment(APP1, exif_segment()), Segment(APP1, xmp_segment())]
    out = _write(kept)
    audit(out)
    return out


def audit(data: bytes) -> None:
    """Raise ``JpegError`` unless *data* is a clean JPEG: only the segments of the white list, in
    a sane order, once where once is the rule, and nothing after the EOI."""
    segments = parse(data)
    count: Dict[str, int] = {}
    scan_seen = False
    for segment in segments:
        name = _check_segment(segment, scan_seen)
        count[name] = count.get(name, 0) + 1
        scan_seen = scan_seen or segment.marker == SOS
        # the words are looked for in the segments, not in the entropy-coded data
        for word in () if name in ("Exif", "XMP") else FORBIDDEN_WORDS:
            if word in segment.data:
                raise JpegError(f"{word!r} in a {name} segment")
    for name, limit in (
        ("SOF0", 1),
        ("SOS", 1),
        ("DRI", 1),
        ("JFIF", 1),
        ("Adobe", 1),
        ("Exif", 1),
        ("XMP", 1),
    ):
        if count.get(name, 0) > limit:
            raise JpegError(f"{count[name]} {name} segments, at most {limit}")
    for name in ("DQT", "DHT", "SOF0", "SOS", "Exif", "XMP"):
        if not count.get(name):
            raise JpegError(f"no {name} segment")
    if segments[-1].marker != EOI or segments[0].marker != SOI:
        raise JpegError("not SOI ... EOI")  # parse() makes this unreachable; kept as a net


def _check_segment(segment: Segment, scan_seen: bool) -> str:
    marker = segment.marker
    if marker in (SOI, EOI):
        return "SOI" if marker == SOI else "EOI"
    if marker in (DQT, DHT, SOF0, DRI):
        name = {DQT: "DQT", DHT: "DHT", SOF0: "SOF0", DRI: "DRI"}[marker]
        if scan_seen:
            raise JpegError(f"a {name} segment after the scan")
        return name
    if marker == SOS:
        return "SOS"
    if 0xE0 <= marker <= 0xEF and scan_seen:
        raise JpegError(f"an FF{marker:02X} segment after the scan")
    if _is_exif(segment):
        if segment.data != exif_segment():
            raise JpegError("an Exif segment that is not the expected one (Artist and Copyright)")
        return "Exif"
    if _is_xmp(segment):
        if segment.data != xmp_segment():
            raise JpegError("an XMP segment that is not the expected one (creator and rights)")
        return "XMP"
    if _is_jfif(segment):
        if len(segment.data) != 18:  # the length field says 16: no thumbnail, nothing else
            raise JpegError("a JFIF segment with a thumbnail or of an odd length")
        return "JFIF"
    if _is_adobe(segment):
        # FF EE, the length 14, "Adobe", version (2), flags0 (2), flags1 (2), transform (1)
        if len(segment.data) != 16 or int.from_bytes(segment.data[2:4], "big") != 14:
            raise JpegError("an Adobe segment whose length is not 14")
        if segment.data[15] > 2:
            raise JpegError(f"an Adobe segment with the transform {segment.data[15]}")
        return "Adobe"
    if _is_icc(segment):
        raise JpegError("an ICC profile: the pictures carry none (the cleaner takes them out)")
    raise JpegError(f"the segment FF{marker:02X} is not on the white list")


def structure_digest(data: bytes) -> str:
    """SHA-256 of what decodes the picture: every DQT, DHT, SOF0, DRI and SOS segment, in file
    order, then the entropy-coded data. Two files with the same digest give the same coefficients,
    whatever the metadata around them."""
    digest = hashlib.sha256()
    entropy = b""
    for segment in parse(data):
        if segment.marker in (DQT, DHT, SOF0, DRI, SOS):
            digest.update(segment.data)
            entropy += segment.entropy
    digest.update(entropy)
    return digest.hexdigest()


def dimensions(data: bytes) -> Tuple[int, int]:
    """(width, height) from the SOF0 header."""
    for segment in parse(data):
        if segment.marker == SOF0:
            return (
                int.from_bytes(segment.data[7:9], "big"),
                int.from_bytes(segment.data[5:7], "big"),
            )
    raise JpegError("no SOF0 segment")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Strip the metadata of JPEG files losslessly.")
    parser.add_argument("--out", required=True, help="the folder the clean files are written to")
    parser.add_argument("sources", nargs="+")
    args = parser.parse_args(argv)
    os.makedirs(args.out, exist_ok=True)
    for source in args.sources:
        target = os.path.join(args.out, os.path.basename(source))
        with open(source, "rb") as handle:
            original = handle.read()
        result = clean(original)
        with open(target, "xb") as handle:  # "x": an existing file is never overwritten
            handle.write(result)
        print(f"{os.path.basename(source)}: {len(original)} -> {len(result)} bytes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
