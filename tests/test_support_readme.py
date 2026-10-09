"""The README and the donation link go together (``slideshow_lock/about.py``, ``DONATION_URL``).

The README has a ``## Support`` section only when the constant is a valid ``https://`` address,
and then it holds that address; the placeholder ``<DONATION_URL>`` is never in the README. The
commit that turns the link on is one small one: the constant and the README section. These tests
fail for either half alone.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from slideshow_lock import about
from tests.test_about import _readme_section

README = Path(__file__).resolve().parent.parent / "README.md"


def support_state(readme, url):
    """None if the README and *url* agree, else what is wrong. The README has a ``## Support``
    section only for a valid *url*, has that address in the section, and never the placeholder."""
    if "<DONATION_URL>" in readme:
        return "the placeholder is in the README"
    section = _readme_section(readme, "Support")
    valid = about.donation_link(url) is not None
    if section is None:
        return "valid address but no Support section" if valid else None
    if not valid:
        return "a Support section with no valid address"
    return None if section.count(url) == 1 else "the section does not hold the address once"


def test_the_readme_has_a_support_section_only_for_a_valid_address():
    readme = README.read_text(encoding="utf-8")
    assert support_state(readme, about.DONATION_URL) is None
    assert ("## Support" in readme) == (about.donation_link() is not None)
    assert "<DONATION_URL>" not in readme


GOOD_README = "# x\n\n## Authorship\n\nText.\n"
SUPPORT = "\n## Support\n\nHelp: https://donate.example.org/x\n"


@pytest.mark.parametrize(
    "readme,url,expected",
    [
        (GOOD_README, "", None),
        (GOOD_README, "<DONATION_URL>", None),
        (GOOD_README + SUPPORT, "https://donate.example.org/x", None),
        (GOOD_README, "https://donate.example.org/x", "valid address but no Support section"),
        (GOOD_README + SUPPORT, "", "a Support section with no valid address"),
        (GOOD_README + SUPPORT, "<DONATION_URL>", "a Support section with no valid address"),
        (
            GOOD_README + SUPPORT,
            "http://donate.example.org/x",
            "a Support section with no valid address",
        ),
        (
            GOOD_README + SUPPORT,
            "https://other.example.org/x",
            "the section does not hold the address once",
        ),
        (GOOD_README + "\nSupport: <DONATION_URL>\n", "", "the placeholder is in the README"),
        (
            GOOD_README + "\n<!-- ## Support <DONATION_URL> -->\n",
            "",
            "the placeholder is in the README",
        ),
    ],
)
def test_the_readme_check_finds_each_way_the_two_can_disagree(readme, url, expected):
    assert support_state(readme, url) == expected
