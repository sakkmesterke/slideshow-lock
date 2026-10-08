"""The sample pictures in ``data/pictures``: clean, whole, listed, and small enough.

The pictures are JPEG files that went through ``tools/strip_jpeg_metadata.py``: the Photoshop
block, comments, every ICC profile and every other piece of metadata are out, the picture itself is
byte for byte what it was. No colour profile is in: the bytes of a profile are not the author's, so
the licence of the pictures could not cover them. Two pieces are in, on purpose: the author and
the licence, as one Exif APP1 (Artist, Copyright) and one XMP APP1 (dc:creator, dc:rights,
xmpRights:WebStatement), byte for byte what ``exif_segment()`` and ``xmp_segment()`` of the tool
give. This module keeps it so, for this set and for any set that replaces it (a later release must
not bring personal data back in):

* ``audit`` (the white list of segments, closed: it fails on anything it does not understand) over
  every picture, one test case for each picture and each of the missing or changed fields, and the
  cleaner over pictures that are made dirty on purpose;
* the content gate of the folder (only ``CREDITS.txt`` and pictures with a valid name, the right
  magic, a size the picture reader takes, no link, no execute bit), the manifest
  ``packaging/pictures.sha256`` and the size gate (the 50 000 000 bytes at which the single package
  has to be split);
* the real folder through ``sample_pictures.install``, as a package would hand it over.

Every gate is a function that takes a folder, so the same code that runs on ``data/pictures`` runs
on a folder that is broken on purpose. Standard library only: no GTK.
"""

from __future__ import annotations

import functools
import hashlib
import importlib.util
import os
import re
import stat
import struct
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Callable, Dict, List

import pytest

from slideshow_lock import sample_pictures as sp
from slideshow_lock.image_source import IMAGE_EXTENSIONS
from slideshow_lock.scaling import MAX_FILE_BYTES, MAX_PIXELS

REPO = Path(__file__).resolve().parent.parent
PICTURES = REPO / "data" / "pictures"
MANIFEST = REPO / "packaging" / "pictures.sha256"
README = REPO / "README.md"

_spec = importlib.util.spec_from_file_location(
    "strip_jpeg_metadata", REPO / "tools" / "strip_jpeg_metadata.py"
)
sj = importlib.util.module_from_spec(_spec)
sys.modules["strip_jpeg_metadata"] = sj
_spec.loader.exec_module(sj)

#: The credit line: one text, in ``CREDITS.txt`` and in the README (no year, no path, no host name).
CREDITS_TEXT = "Fraktálképek: sakkmesterke (Alexovics Attila), CC BY-SA 4.0\n"

#: The texts as they were given, written out here once more on purpose: the tool's constants are
#: compared with them (the test is not the tool's own output compared with itself).
AUTHOR_TEXT = "sakkmesterke (Alexovics Attila)"
RIGHTS_TEXT = "CC BY-SA 4.0, sakkmesterke (Alexovics Attila)"
WEB_STATEMENT_TEXT = "https://creativecommons.org/licenses/by-sa/4.0/"

#: The whole Exif APP1 segment of every picture (126 bytes) and the XMP APP1 segment (687 bytes).
EXPECTED_EXIF_HEX = (
    "ffe1007c45786966000049492a000800000002003b0102002000000026000000988202002e0000004600"
    "00000000000073616b6b6d65737465726b652028416c65786f7669637320417474696c61290043432042"
    "592d534120342e302c2073616b6b6d65737465726b652028416c65786f7669637320417474696c612900"
)
EXPECTED_XMP_PAYLOAD = (
    '<?xpacket begin="\ufeff" id="W5M0MpCehiHzreSzNTczkc9d"?>'
    '<x:xmpmeta xmlns:x="adobe:ns:meta/">'
    '<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
    '<rdf:Description rdf:about="" xmlns:dc="http://purl.org/dc/elements/1.1/"'
    ' xmlns:xmpRights="http://ns.adobe.com/xap/1.0/rights/">'
    "<dc:creator><rdf:Seq><rdf:li>sakkmesterke (Alexovics Attila)</rdf:li></rdf:Seq></dc:creator>"
    '<dc:rights><rdf:Alt><rdf:li xml:lang="x-default">CC BY-SA 4.0, sakkmesterke (Alexovics '
    "Attila)</rdf:li></rdf:Alt></dc:rights>"
    "<xmpRights:WebStatement>https://creativecommons.org/licenses/by-sa/4.0/"
    "</xmpRights:WebStatement>"
    "</rdf:Description></rdf:RDF></x:xmpmeta>"
    '<?xpacket end="w"?>'
)

#: The size at which one package is no longer the answer (docs of the plan: a subpackage then).
SIZE_LIMIT_BYTES = 50_000_000

JPEG_MAGIC = b"\xff\xd8\xff"


def image_files(folder: Path) -> List[Path]:
    return sorted(p for p in folder.iterdir() if p.suffix.lower() in IMAGE_EXTENSIONS)


# -- the gates, each on a folder -----------------------------------------------------------------


def content_problems(folder: Path) -> List[str]:
    """What is wrong with the files of *folder* as a package would ship them (empty: nothing)."""
    problems = []
    for entry in sorted(folder.iterdir()):
        name, info = entry.name, os.lstat(entry)
        if stat.S_ISLNK(info.st_mode):
            problems.append(f"{name}: a link")
            continue
        if not stat.S_ISREG(info.st_mode):
            problems.append(f"{name}: not a regular file")
            continue
        if info.st_mode & 0o111:
            problems.append(f"{name}: has an execute bit")
        if name == sp.CREDITS_NAME:
            continue
        if not sp._valid_name(name):
            problems.append(f"{name}: not a valid name")
        if not sp.is_image_name(name) or name != name.lower():
            problems.append(f"{name}: not a picture name in lower case")
        with open(entry, "rb") as handle:
            head = handle.read(3)
        if os.path.splitext(name)[1] in (".jpg", ".jpeg") and head != JPEG_MAGIC:
            problems.append(f"{name}: no JPEG magic")
        if info.st_size > MAX_FILE_BYTES:
            problems.append(f"{name}: larger than the picture reader takes")
            continue
        try:
            width, height = sj.dimensions(entry.read_bytes())
        except sj.JpegError as exc:
            problems.append(f"{name}: {exc}")
            continue
        if width * height > MAX_PIXELS:
            problems.append(f"{name}: {width}x{height} is more than the pixel limit")
    return problems


def size_problem(folder: Path) -> str:
    """Empty, or the message of the size gate: the bytes of the pictures (the .txt left out)."""
    total = sum(os.lstat(p).st_size for p in image_files(folder))
    if total >= SIZE_LIMIT_BYTES:
        return (
            f"the pictures add up to {total} bytes, the limit is {SIZE_LIMIT_BYTES}: the "
            "threshold is reached, a decision is needed (a subpackage), do not raise the number "
            "in silence"
        )
    return ""


def manifest_problems(folder: Path, manifest_text: str) -> List[str]:
    """The pictures of *folder* against the ``sha256sum`` lines of *manifest_text*."""
    listed = {}
    problems = []
    for line in manifest_text.splitlines():
        match = re.fullmatch(r"([0-9a-f]{64})  ([^/\s]+)", line)
        if not match:
            problems.append(f"not a sha256sum line: {line!r}")
        elif match.group(2) in listed:
            problems.append(f"{match.group(2)} twice")
        else:
            listed[match.group(2)] = match.group(1)
    found = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in image_files(folder)}
    for name in sorted(set(found) - set(listed)):
        problems.append(f"{name}: not in the manifest")
    for name in sorted(set(listed) - set(found)):
        problems.append(f"{name}: in the manifest, not in the folder")
    for name in sorted(set(found) & set(listed)):
        if found[name] != listed[name]:
            problems.append(f"{name}: the hash differs")
    return problems


# -- the pictures that are in the repository -----------------------------------------------------


def test_there_are_pictures_and_a_credits_file():
    assert image_files(PICTURES), "data/pictures has no picture: the package would be empty"
    assert (PICTURES / sp.CREDITS_NAME).is_file()


def test_the_texts_are_the_given_ones_and_one_source_makes_all_of_them():
    assert (sj.AUTHOR, sj.RIGHTS, sj.LICENSE_URL) == (AUTHOR_TEXT, RIGHTS_TEXT, WEB_STATEMENT_TEXT)
    assert sj.CREDITS_LINE == CREDITS_TEXT
    assert sj.exif_segment().hex() == EXPECTED_EXIF_HEX
    assert (
        sj.xmp_segment()
        == b"\xff\xe1"
        + (len(sj.XMP_ID) + 2 + len(EXPECTED_XMP_PAYLOAD.encode())).to_bytes(2, "big")
        + sj.XMP_ID
        + EXPECTED_XMP_PAYLOAD.encode()
    )


def read_exif(segment: bytes) -> dict:
    """The tags of the Exif APP1 segment, read by a TIFF reader of its own (not the tool's code):
    ``{tag: text}`` of IFD0, and the checks that the structure is the minimal one."""
    assert segment[:2] == b"\xff\xe1" and segment[4:10] == b"Exif\x00\x00"
    assert int.from_bytes(segment[2:4], "big") == len(segment) - 2
    tiff = segment[10:]
    order = {b"II": "little", b"MM": "big"}[tiff[:2]]
    assert int.from_bytes(tiff[2:4], order) == 42
    ifd = int.from_bytes(tiff[4:8], order)
    count = int.from_bytes(tiff[ifd : ifd + 2], order)
    tags, end = {}, ifd + 2 + 12 * count
    for i in range(count):
        entry = tiff[ifd + 2 + 12 * i : ifd + 14 + 12 * i]
        tag, kind, length = (int.from_bytes(entry[a:b], order) for a, b in ((0, 2), (2, 4), (4, 8)))
        assert kind == 2, "ASCII only: no pointer to another IFD, no number"
        offset = int.from_bytes(entry[8:12], order)
        raw = tiff[offset : offset + length] if length > 4 else entry[8 : 8 + length]
        assert raw.endswith(b"\x00") and b"\x00" not in raw[:-1]
        tags[tag] = raw[:-1].decode("ascii")
    assert int.from_bytes(tiff[end : end + 4], order) == 0, "a next IFD (a thumbnail)"
    return tags


def read_xmp(segment: bytes) -> dict:
    """The fields of the XMP APP1 segment, read with an XML parser: ``{"creator": ..}`` and the
    list of the property names, nothing else may be in it."""
    assert segment[:2] == b"\xff\xe1" and segment[4 : 4 + len(sj.XMP_ID)] == sj.XMP_ID
    text = segment[4 + len(sj.XMP_ID) :].decode("utf-8")
    root = ET.fromstring(text[text.index("<x:xmpmeta") : text.index("</x:xmpmeta>") + 12])
    ns = {"rdf": "http://www.w3.org/1999/02/22-rdf-syntax-ns#"}
    (description,) = root.findall("./rdf:RDF/rdf:Description", ns)
    return {child.tag: "".join(child.itertext()).strip() for child in description}


def test_the_independent_readers_read_the_expected_fields_and_nothing_more():
    assert read_exif(sj.exif_segment()) == {0x013B: AUTHOR_TEXT, 0x8298: RIGHTS_TEXT}
    assert read_xmp(sj.xmp_segment()) == {
        "{http://purl.org/dc/elements/1.1/}creator": AUTHOR_TEXT,
        "{http://purl.org/dc/elements/1.1/}rights": RIGHTS_TEXT,
        "{http://ns.adobe.com/xap/1.0/rights/}WebStatement": WEB_STATEMENT_TEXT,
    }


@pytest.mark.parametrize("picture", image_files(PICTURES), ids=lambda p: p.name)
def test_a_picture_has_one_exif_and_one_xmp_of_the_author_and_nothing_else(picture):
    data = picture.read_bytes()
    sj.audit(data)  # the white list; raises on anything else
    segments = sj.parse(data)
    app1 = [s.data for s in segments if s.marker == sj.APP1]
    exif = [d for d in app1 if d[4:10] == b"Exif\x00\x00"]
    xmp = [d for d in app1 if d[4 : 4 + len(sj.XMP_ID)] == sj.XMP_ID]
    assert (len(app1), len(exif), len(xmp)) == (2, 1, 1)  # every other APP1 would be a third
    assert exif == [sj.exif_segment()] and xmp == [sj.xmp_segment()]  # byte for byte
    assert read_exif(exif[0]) == {0x013B: AUTHOR_TEXT, 0x8298: RIGHTS_TEXT}  # and read again
    assert read_xmp(xmp[0])["{http://purl.org/dc/elements/1.1/}rights"] == RIGHTS_TEXT
    markers = {segment.marker for segment in segments}
    assert not markers & {sj.APP13, sj.COM}  # no Photoshop block, no comment


def app_segments(data: bytes) -> List[tuple]:
    """(marker, first 12 payload bytes) of every segment before the scan, read with no code of the
    tool: the length field of each segment says where the next one starts."""
    found, pos = [], 2
    while data[pos + 1] != 0xDA:  # up to SOS
        length = int.from_bytes(data[pos + 2 : pos + 4], "big")
        found.append((data[pos + 1], data[pos + 4 : pos + 16]))
        pos += 2 + length
    return found


@pytest.mark.parametrize("picture", image_files(PICTURES), ids=lambda p: p.name)
def test_a_picture_carries_no_colour_profile_of_anybody(picture):
    data = picture.read_bytes()
    segments = app_segments(data)
    assert segments  # the walker did walk
    assert not [m for m, head in segments if m == 0xE2]  # no APP2 at all (ICC, MPF, FlashPix)
    assert not [m for m, head in segments if head.startswith(b"ICC_PROFILE")]
    assert b"ICC_PROFILE" not in data[: data.index(b"\xff\xda")]  # nowhere in the headers


def test_the_folder_passes_the_content_gate_the_manifest_and_the_size_gate():
    assert content_problems(PICTURES) == []
    assert manifest_problems(PICTURES, MANIFEST.read_text()) == []
    assert size_problem(PICTURES) == ""


def test_the_credits_are_the_fixed_line_without_a_year_a_path_or_a_host():
    text = (PICTURES / sp.CREDITS_NAME).read_text(encoding="utf-8")
    assert text == CREDITS_TEXT
    assert not re.search(r"\b(19|20)\d\d\b", text) and "/" not in text and "\\" not in text
    assert CREDITS_TEXT.strip() in README.read_text(encoding="utf-8")


def test_the_real_folder_is_taken_over_by_install_with_the_credits_and_the_bytes(tmp_path):
    """The folder as the package installs it, through the code that copies it."""
    result = sp.install(
        str(PICTURES), str(tmp_path / "Pictures"), str(tmp_path / "state" / "sample-pictures.json")
    )
    assert (result.status, result.copied) == (sp.DONE, len(os.listdir(PICTURES)))
    copied = tmp_path / "Pictures" / sp.SUBDIR
    assert sorted(os.listdir(copied)) == sorted(os.listdir(PICTURES))
    assert manifest_problems(copied, MANIFEST.read_text()) == []
    assert (copied / sp.CREDITS_NAME).read_bytes() == (PICTURES / sp.CREDITS_NAME).read_bytes()


# -- negative controls of the content, manifest and size gates -----------------------------------


def make_folder(tmp_path: Path) -> Path:
    """A folder with two real pictures and the credits."""
    folder = tmp_path / "pictures"
    folder.mkdir()
    for picture in image_files(PICTURES)[:2]:
        (folder / picture.name).write_bytes(picture.read_bytes())
    (folder / sp.CREDITS_NAME).write_bytes((PICTURES / sp.CREDITS_NAME).read_bytes())
    return folder


def patched_size(data: bytes, width: int, height: int) -> bytes:
    """*data* with the width and height of its SOF0 header changed (the picture is not decoded)."""
    segment = next(s for s in sj.parse(data) if s.marker == sj.SOF0)
    start = data.index(segment.data)
    new = segment.data[:5] + height.to_bytes(2, "big") + width.to_bytes(2, "big") + segment.data[9:]
    return data[:start] + new + data[start + len(segment.data) :]


def test_the_gates_are_quiet_on_a_folder_that_is_fine(tmp_path):
    folder = make_folder(tmp_path)
    assert content_problems(folder) == []
    assert size_problem(folder) == ""
    listing = "".join(
        f"{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.name}\n" for p in image_files(folder)
    )
    assert manifest_problems(folder, listing) == []


def _break_link(folder):
    os.symlink("/etc/hostname", folder / "link.jpg")


def _break_exec(folder):
    os.chmod(folder / image_files(folder)[0].name, 0o755)


def _break_extra_txt(folder):
    (folder / "notes.txt").write_text("x")


def _break_hidden(folder):
    (folder / ".hidden.jpg").write_bytes(JPEG_MAGIC + b"\x00")


def _break_magic(folder):
    (folder / "png.jpg").write_bytes(b"\x89PNG\r\n\x1a\n")


def _break_upper_case(folder):
    (folder / "FR09.JPG").write_bytes((folder / image_files(folder)[0].name).read_bytes())


def _break_control_char(folder):
    (folder / "a\x01b.jpg").write_bytes((folder / image_files(folder)[0].name).read_bytes())


def _break_subfolder(folder):
    (folder / "sub").mkdir()


def _break_pixels(folder):
    first = folder / image_files(folder)[0].name
    first.write_bytes(patched_size(first.read_bytes(), 10000, 10000))


def _break_file_size(folder):
    with open(folder / "huge.jpg", "wb") as handle:
        handle.write(JPEG_MAGIC)
        handle.truncate(MAX_FILE_BYTES + 1)


@pytest.mark.parametrize(
    "mutant, expected",
    [
        (_break_link, "a link"),
        (_break_exec, "execute bit"),
        (_break_extra_txt, "notes.txt: not a picture name"),
        (_break_hidden, "not a valid name"),
        (_break_magic, "no JPEG magic"),
        (_break_upper_case, "not a picture name in lower case"),
        (_break_control_char, "not a valid name"),
        (_break_subfolder, "not a regular file"),
        (_break_pixels, "more than the pixel limit"),
        (_break_file_size, "larger than the picture reader takes"),
    ],
    ids=lambda x: x if isinstance(x, str) else x.__name__,
)
def test_the_content_gate_reports_each_kind_of_defect(tmp_path, mutant, expected):
    folder = make_folder(tmp_path)
    mutant(folder)
    assert any(expected in line for line in content_problems(folder)), content_problems(folder)


def test_the_size_gate_is_red_at_the_limit_and_quiet_one_byte_below(tmp_path):
    folder = tmp_path / "pictures"
    folder.mkdir()
    big = folder / "big.jpg"
    with open(big, "wb") as handle:
        handle.truncate(SIZE_LIMIT_BYTES - 1)
    (folder / sp.CREDITS_NAME).write_bytes(b"x" * 1000)  # the .txt is not counted
    assert size_problem(folder) == ""
    with open(big, "ab") as handle:
        handle.truncate(SIZE_LIMIT_BYTES)
    assert "a decision is needed" in size_problem(folder)
    assert "do not raise the number" in size_problem(folder)


def test_the_manifest_gate_reports_a_changed_a_missing_and_an_extra_picture(tmp_path):
    folder = make_folder(tmp_path)
    good = MANIFEST.read_text()
    first, second = (p.name for p in image_files(folder))
    digests = {n: hashlib.sha256((folder / n).read_bytes()).hexdigest() for n in (first, second)}
    lines = {name: f"{digest}  {name}\n" for name, digest in digests.items()}
    ok = "".join(lines.values())
    assert manifest_problems(folder, ok) == []
    changed = ok.replace(digests[first], "f" * 64 if digests[first][0] != "f" else "e" * 64)
    assert any("hash differs" in p for p in manifest_problems(folder, changed))
    assert any("not in the manifest" in p for p in manifest_problems(folder, lines[first]))
    assert any(
        "not in the folder" in p for p in manifest_problems(folder, ok + "0" * 64 + "  gone.jpg\n")
    )
    assert any("not a sha256sum line" in p for p in manifest_problems(folder, ok + "oops\n"))
    assert any("twice" in p for p in manifest_problems(folder, ok + lines[first]))
    assert (
        manifest_problems(folder, good) != []
    )  # the manifest of the real set is not this folder's


# -- the white list: the cleaner and the audit, on pictures made dirty on purpose -----------------


def app(marker: int, payload: bytes) -> bytes:
    return b"\xff" + bytes([marker]) + (len(payload) + 2).to_bytes(2, "big") + payload


EXIF = app(sj.APP1, b"Exif\x00\x00II*\x00\x08\x00\x00\x00" + b"\x00" * 40 + b"Photopea")
XMP = app(
    sj.APP1,
    b"http://ns.adobe.com/xap/1.0/\x00<x:xmpmeta><xmp:CreatorTool>x</xmp:CreatorTool></x:xmpmeta>",
)
PHOTOSHOP = app(sj.APP13, b"Photoshop 3.0\x008BIM\x04\x04\x00\x00\x00\x00\x00\x04IPTC")
COMMENT = app(sj.COM, b"made at /home/someone/Pictures")
MPF = app(sj.APP2, b"MPF\x00II*\x00\x08\x00\x00\x00")
#: A colour profile the way a camera or an editor writes it: the 128-byte header (the signature
#: ``acsp`` at 36) and a tag count; the profile is not a real one, the cleaner does not read it.
ICC_BODY = b"\x00" * 36 + b"acsp" + b"\x00" * 88 + b"\x00\x00\x00\x00"
ICC = app(sj.APP2, b"ICC_PROFILE\x00\x01\x01" + ICC_BODY)
ICC_CHUNKS = app(sj.APP2, b"ICC_PROFILE\x00\x01\x02" + ICC_BODY) + app(
    sj.APP2, b"ICC_PROFILE\x00\x02\x02" + ICC_BODY
)
OTHER_APP = app(0xE5, b"Ducky\x00\x01")
XMP_EXTENSION = app(sj.APP1, b"http://ns.adobe.com/xmp/extension/\x00" + b"0" * 32 + b"\x00" * 8)


def real(name: str) -> bytes:
    return (PICTURES / name).read_bytes()


def sample_names() -> List[str]:
    """One picture with JFIF (the usual) and one with Adobe APP14 and a restart interval, if the
    set has them; the clean-up tests below run on every picture of the set."""
    return [p.name for p in image_files(PICTURES)]


def segment_bytes(data: bytes, marker: int) -> bytes:
    return next(s for s in sj.parse(data) if s.marker == marker).data


def after_soi(data: bytes, extra: bytes) -> bytes:
    return data[:2] + extra + data[2:]


@pytest.mark.parametrize("name", sample_names())
@pytest.mark.parametrize(
    "dirt",
    [
        EXIF + XMP,
        PHOTOSHOP + COMMENT,
        MPF + OTHER_APP,
        ICC,
        ICC_CHUNKS,
        EXIF + XMP + PHOTOSHOP + COMMENT + MPF + ICC,
    ],
    ids=["exif+xmp", "photoshop+com", "mpf+app5", "icc", "icc in two chunks", "all of it"],
)
def test_the_cleaner_takes_the_dirt_out_and_gives_back_the_same_bytes(name, dirt):
    clean = real(name)
    assert sj.clean(after_soi(clean, dirt)) == clean
    # the same dirt at the end of the headers, between the tables and the frame
    first_dqt = segment_bytes(clean, sj.DQT)
    at = clean.index(first_dqt) + len(first_dqt)
    assert sj.clean(clean[:at] + dirt + clean[at:]) == clean


@pytest.mark.parametrize("name", sample_names())
def test_a_clean_picture_is_not_changed_by_the_cleaner(name):
    assert sj.clean(real(name)) == real(name)


@functools.lru_cache(maxsize=None)
def mutants():
    """name -> (file, bytes made from it, the part of the message that must name the cause)."""
    first = sample_names()[0]
    data = real(first)
    sof, sos = segment_bytes(data, sj.SOF0), segment_bytes(data, sj.SOS)
    jfif = segment_bytes(data, sj.APP0)
    dqt = segment_bytes(data, sj.DQT)
    scan = sos + b"\x12\x34"
    adobe14 = b"\xff\xee\x00\x0eAdobe\x00\x64\x00\x00\x00\x00\x01"
    return {
        "exif": (data, after_soi(data, EXIF), r"Exif segment that is not the expected"),
        "xmp": (data, after_soi(data, XMP), r"XMP segment that is not the expected"),
        "a second exif, the expected one": (
            data,
            after_soi(data, sj.exif_segment()),
            r"2 Exif segments",
        ),
        "a second xmp, the expected one": (
            data,
            after_soi(data, sj.xmp_segment()),
            r"2 XMP segments",
        ),
        "an XMP extension": (data, after_soi(data, XMP_EXTENSION), r"FFE1.*not on the white list"),
        "an exif after the scan": (
            data,
            data[:-2] + sj.exif_segment() + data[-2:],
            r"FFE1 segment after the scan",
        ),
        "photoshop": (data, after_soi(data, PHOTOSHOP), r"FFED.*not on the white list"),
        "comment": (data, after_soi(data, COMMENT), r"FFFE.*not on the white list"),
        "mpf": (data, after_soi(data, MPF), r"FFE2.*not on the white list"),
        "other app": (data, after_soi(data, OTHER_APP), r"FFE5.*not on the white list"),
        "second jfif": (data, after_soi(data, jfif), r"2 JFIF"),
        "jfif with a thumbnail": (
            data,
            data.replace(jfif, jfif[:2] + b"\x00\x12" + jfif[4:] + b"\x00\x00", 1),
            r"JFIF segment with a thumbnail",
        ),
        "adobe: transform 3": (data, after_soi(data, adobe14[:-1] + b"\x03"), r"transform 3"),
        "adobe: length 13": (
            data,
            after_soi(data, b"\xff\xee\x00\x0dAdobe\x00\x64\x00\x00\x00\x00"),
            r"length is not 14",
        ),
        "adobe: length 15": (
            data,
            after_soi(data, b"\xff\xee\x00\x0fAdobe\x00\x64\x00\x00\x00\x00\x01\x00"),
            r"length is not 14",
        ),
        "two adobe": (data, after_soi(after_soi(data, adobe14), adobe14), r"2 Adobe"),
        "data after the EOI": (data, data + b"\x00", r"1 bytes after the EOI"),
        "a picture after the EOI": (data, data + data, r"bytes after the EOI"),
        "second SOF": (data, data.replace(sof, sof + sof, 1), r"2 SOF0"),
        "progressive frame": (
            data,
            data.replace(sof, sof[:1] + b"\xc2" + sof[2:], 1),
            r"FFC2.*not on the white list",
        ),
        "second SOS": (data, data[:-2] + scan + data[-2:], r"2 SOS"),
        "a table after the scan": (
            data,
            data[:-2] + dqt + data[-2:],
            r"DQT segment after the scan",
        ),
        "an ICC profile": (data, after_soi(data, ICC), r"an ICC profile: the pictures carry none"),
        "an ICC profile in two chunks": (
            data,
            after_soi(data, ICC_CHUNKS),
            r"an ICC profile: the pictures carry none",
        ),
        "an ICC profile after the JFIF header": (
            data,
            data.replace(jfif, jfif + ICC, 1),
            r"an ICC profile: the pictures carry none",
        ),
        "an ICC profile after the scan": (
            data,
            data[:-2] + ICC + data[-2:],
            r"FFE2 segment after the scan",
        ),
        "no EOI": (data, data[:-2], r"no EOI"),
        "cut inside the scan": (data, data[: len(data) // 2], r"no EOI"),
        "no SOI": (data, data[1:], r"no SOI"),
        "a length below 2": (
            data,
            data.replace(dqt, dqt[:2] + b"\x00\x01" + dqt[4:], 1),
            r"has the length 1",
        ),
        "a length over the end": (data, data[:30], r"FFE1 at offset 20 has the length"),
        "a fill byte before a marker": (
            data,
            data.replace(sof, b"\xff" + sof, 1),
            r"where a segment is expected",
        ),
        "a forbidden word in a table": (
            data,
            data.replace(dqt, dqt[:8] + b"Photoshop" + dqt[17:], 1),
            r"Photoshop.*DQT",
        ),
    }


@pytest.mark.parametrize("case", sorted(mutants()))
def test_the_audit_is_red_for_each_kind_of_dirt_and_names_it(case):
    original, mutant, message = mutants()[case]
    sj.audit(original)  # the picture it was made from is clean: only the mutation is to blame
    with pytest.raises(sj.JpegError, match=message):
        sj.audit(mutant)


def test_the_audit_does_not_search_the_entropy_data_for_words():
    data = real(sample_names()[0])
    sos = segment_bytes(data, sj.SOS)
    at = data.index(sos) + len(sos) + 100
    assert b"\xff" not in data[at : at + 9]
    sj.audit(data[:at] + b"Photoshop" + data[at + 9 :])


def test_the_cleaner_takes_every_profile_out_and_refuses_a_file_it_does_not_understand():
    data = real(sample_names()[0])
    for profiled in (after_soi(data, ICC), after_soi(data, ICC_CHUNKS)):
        with pytest.raises(sj.JpegError, match="an ICC profile: the pictures carry none"):
            sj.audit(profiled)  # the audit is red for it ...
        assert sj.clean(profiled) == data  # ... and the cleaner takes it out, puts none back
    with pytest.raises(sj.JpegError):
        sj.clean(data[:-2])  # no EOI
    with pytest.raises(sj.JpegError):
        sj.clean(b"not a jpeg")


def test_the_command_writes_clean_copies_and_never_overwrites(tmp_path, capsys):
    source = tmp_path / "in.jpg"
    source.write_bytes(after_soi(real(sample_names()[0]), EXIF + XMP + PHOTOSHOP))
    out = tmp_path / "out"
    assert sj.main(["--out", str(out), str(source)]) == 0
    assert (out / "in.jpg").read_bytes() == real(sample_names()[0])
    assert "in.jpg:" in capsys.readouterr().out
    with pytest.raises(FileExistsError):
        sj.main(["--out", str(out), str(source)])


@pytest.mark.parametrize("name", sample_names())
def test_the_digest_of_the_picture_does_not_change_with_the_dirt(name):
    clean = real(name)
    assert sj.structure_digest(after_soi(clean, EXIF + XMP + PHOTOSHOP + COMMENT)) == (
        sj.structure_digest(clean)
    )
    other = sample_names()[(sample_names().index(name) + 1) % len(sample_names())]
    if other != name:
        assert sj.structure_digest(real(other)) != sj.structure_digest(clean)


def test_the_digest_changes_with_one_byte_of_the_entropy_data_or_of_a_table():
    data = real(sample_names()[0])
    sos = segment_bytes(data, sj.SOS)
    at = data.index(sos) + len(sos) + 100
    assert data[at] not in (0x00, 0xFE, 0xFF) and data[at - 1] != 0xFF
    entropy = data[:at] + bytes([data[at] ^ 1]) + data[at + 1 :]
    sj.audit(entropy)  # still a well-formed file: only the digest is to tell
    assert sj.structure_digest(entropy) != sj.structure_digest(data)
    dqt = data.index(segment_bytes(data, sj.DQT)) + 8
    table = data[:dqt] + bytes([data[dqt] ^ 1]) + data[dqt + 1 :]
    assert sj.structure_digest(table) != sj.structure_digest(data)


# -- the author and the licence in every picture: each field, each picture ------------------------


def exif_with_a_sub_ifd(kind: str) -> bytes:
    """An Exif segment of the same two tags and one thing more that must not be there: a GPS IFD,
    an Exif IFD or a next IFD (the thumbnail), as the cameras write them."""
    base = sj.exif_segment()
    tiff = bytearray(base[10:])
    count = int.from_bytes(tiff[8:10], "little")
    assert count == 2
    pointer = {"gps": 0x8825, "exif ifd": 0x8769}.get(kind)
    if pointer is not None:
        # a third entry (LONG, one value) that points to an empty IFD at the end; the value
        # offsets move 12 bytes on, so they are written again
        entries = bytes(tiff[10:34])
        values = bytes(tiff[38:])
        fixed = b""
        for i in range(2):
            tag, typ, n, off = struct.unpack("<HHII", entries[12 * i : 12 * i + 12])
            fixed += struct.pack("<HHII", tag, typ, n, off + 12)
        end = 8 + 2 + 36 + 4 + len(values)
        third = struct.pack("<HHII", pointer, 4, 1, end)
        tiff = bytearray(
            b"II*\x00\x08\x00\x00\x00"
            + struct.pack("<H", 3)
            + fixed
            + third
            + struct.pack("<I", 0)
            + values
            + struct.pack("<H", 0)
            + struct.pack("<I", 0)
        )
    else:  # "next ifd": the IFD0 says that an IFD1 follows
        tiff[34:38] = struct.pack("<I", len(tiff))
        tiff += struct.pack("<H", 0) + struct.pack("<I", 0)
    payload = b"Exif\x00\x00" + bytes(tiff)
    return b"\xff\xe1" + (len(payload) + 2).to_bytes(2, "big") + payload


def xmp_changed(change: Callable[[str], str]) -> bytes:
    payload = sj.xmp_segment()[4:].decode("utf-8")
    new = change(payload)
    assert new != payload, "the change did not change anything"
    raw = new.encode("utf-8")
    return b"\xff\xe1" + (len(raw) + 2).to_bytes(2, "big") + raw


def cut(payload: str, start: str, end: str) -> str:
    return payload[: payload.index(start)] + payload[payload.index(end) + len(end) :]


def exif_cases() -> Dict[str, bytes]:
    artist, copyright_ = sj.EXIF_TAGS
    return {
        "Exif Artist missing": sj.exif_segment((copyright_,)),
        "Exif Copyright missing": sj.exif_segment((artist,)),
        "Exif Artist is another text": sj.exif_segment(((artist[0], "someone"), copyright_)),
        "Exif Copyright is another text": sj.exif_segment((artist, (copyright_[0], "CC BY 4.0"))),
        "Exif with one tag more (Software)": sj.exif_segment(
            ((0x0131, "Photopea Editor"), artist, copyright_)
        ),
        "Exif with a GPS IFD": exif_with_a_sub_ifd("gps"),
        "Exif with an Exif IFD": exif_with_a_sub_ifd("exif ifd"),
        "Exif with a next IFD (thumbnail)": exif_with_a_sub_ifd("next ifd"),
    }


def xmp_cases() -> Dict[str, bytes]:
    return {
        "XMP creator missing": xmp_changed(lambda p: cut(p, "<dc:creator>", "</dc:creator>")),
        "XMP rights missing": xmp_changed(lambda p: cut(p, "<dc:rights>", "</dc:rights>")),
        "XMP WebStatement missing": xmp_changed(
            lambda p: cut(p, "<xmpRights:WebStatement>", "</xmpRights:WebStatement>")
        ),
        "XMP rights differ": xmp_changed(lambda p: p.replace("CC BY-SA 4.0,", "CC BY 4.0,")),
        "XMP creator differs": xmp_changed(lambda p: p.replace("<rdf:li>sakk", "<rdf:li>Sakk")),
        "XMP with CreatorTool left": xmp_changed(
            lambda p: p.replace(
                "</rdf:Description>",
                '<xmp:CreatorTool xmlns:xmp="http://ns.adobe.com/xap/1.0/">Photopea</xmp:CreatorTool>'
                "</rdf:Description>",
            )
        ),
        "XMP with History left": xmp_changed(
            lambda p: p.replace(
                "</rdf:Description>",
                '<xmpMM:History xmlns:xmpMM="http://ns.adobe.com/xap/1.0/mm/"><rdf:Seq/>'
                "</xmpMM:History></rdf:Description>",
            )
        ),
    }


@pytest.mark.parametrize("name", sample_names())
@pytest.mark.parametrize("missing", ["Exif", "XMP"])
def test_a_picture_without_its_exif_or_its_xmp_is_red(name, missing):
    data = real(name)
    sj.audit(data)
    segment = sj.exif_segment() if missing == "Exif" else sj.xmp_segment()
    assert data.count(segment) == 1  # the one it is made of
    with pytest.raises(sj.JpegError, match=f"no {missing} segment"):
        sj.audit(data.replace(segment, b"", 1))


@pytest.mark.parametrize("name", sample_names())
@pytest.mark.parametrize("case", sorted(exif_cases()))
def test_a_picture_with_another_exif_than_the_expected_one_is_red(name, case):
    data = real(name)
    mutant = data.replace(sj.exif_segment(), exif_cases()[case], 1)
    assert mutant != data
    with pytest.raises(sj.JpegError, match="Exif segment that is not the expected"):
        sj.audit(mutant)


@pytest.mark.parametrize("name", sample_names())
@pytest.mark.parametrize("case", sorted(xmp_cases()))
def test_a_picture_with_another_xmp_than_the_expected_one_is_red(name, case):
    data = real(name)
    mutant = data.replace(sj.xmp_segment(), xmp_cases()[case], 1)
    assert mutant != data
    with pytest.raises(sj.JpegError, match="XMP segment that is not the expected"):
        sj.audit(mutant)


def test_the_mutants_of_the_exif_are_what_they_say_they_are():
    """Each mutant is a readable Exif of its own kind (so that it is the one thing wrong with it
    that makes it red) and the independent reader tells it from the expected one."""
    expected = {0x013B: AUTHOR_TEXT, 0x8298: RIGHTS_TEXT}
    assert read_exif(exif_cases()["Exif Artist missing"]) == {0x8298: RIGHTS_TEXT}
    assert read_exif(exif_cases()["Exif Copyright missing"]) == {0x013B: AUTHOR_TEXT}
    assert read_exif(exif_cases()["Exif with one tag more (Software)"]) == {
        0x0131: "Photopea Editor",
        **expected,
    }
    for case in (
        "Exif with a GPS IFD",
        "Exif with an Exif IFD",
        "Exif with a next IFD (thumbnail)",
    ):
        with pytest.raises(AssertionError):  # the reader of the tests does not take it either
            read_exif(exif_cases()[case])


def test_the_mutants_of_the_xmp_are_well_formed_xml_with_one_thing_wrong():
    assert "{http://ns.adobe.com/xap/1.0/}CreatorTool" in read_xmp(
        xmp_cases()["XMP with CreatorTool left"]
    )
    assert "{http://purl.org/dc/elements/1.1/}creator" not in read_xmp(
        xmp_cases()["XMP creator missing"]
    )
    assert "{http://purl.org/dc/elements/1.1/}rights" not in read_xmp(
        xmp_cases()["XMP rights missing"]
    )


@pytest.mark.parametrize("name", sample_names())
def test_the_cleaner_puts_the_author_and_the_licence_into_a_picture_that_has_none(name):
    clean = real(name)
    bare = clean.replace(sj.exif_segment(), b"", 1).replace(sj.xmp_segment(), b"", 1)
    assert len(bare) == len(clean) - len(sj.exif_segment()) - len(sj.xmp_segment())
    with pytest.raises(sj.JpegError, match="no Exif segment"):
        sj.audit(bare)
    assert sj.clean(bare) == clean
    other = clean.replace(sj.exif_segment(), exif_cases()["Exif with one tag more (Software)"], 1)
    assert sj.clean(other) == clean


def test_the_cleaner_drops_a_dirty_xmp_and_exif_and_writes_the_expected_ones_once():
    clean = real(sample_names()[0])
    dirty = clean.replace(sj.xmp_segment(), xmp_cases()["XMP with CreatorTool left"], 1)
    out = sj.clean(dirty)
    assert out == clean and out.count(b"CreatorTool") == 0 and out.count(sj.xmp_segment()) == 1
