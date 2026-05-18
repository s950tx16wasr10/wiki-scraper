"""Tests for the images subcommand's offline helpers.

Network code (downloads themselves) is not unit-tested for the same reason
as fetch/wayback. The deterministic pieces are:
    - canonical_filename / file_disk_path: MediaWiki's md5-keyed file layout
    - _parse_cookies: --cookie string parsing
    - _detect_impersonate: pick the curl_cffi TLS profile matching the UA
"""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from scraper import (  # noqa: E402
    _detect_impersonate, _parse_cookies, canonical_filename, file_disk_path,
)


# ----- canonical filename / disk path --------------------------------------

def test_canonical_strips_spaces_and_caps_first():
    assert canonical_filename("foo bar.png") == "Foo_bar.png"


def test_canonical_already_canonical_idempotent():
    assert canonical_filename("Foo_bar.png") == "Foo_bar.png"


def test_canonical_handles_empty():
    assert canonical_filename("") == ""


def test_disk_path_known_value():
    expected_md5 = hashlib.md5(b"Foo.png").hexdigest()
    a, ab, canon = file_disk_path("Foo.png")
    assert canon == "Foo.png"
    assert a == expected_md5[0]
    assert ab == expected_md5[:2]


def test_disk_path_applies_canonicalization_before_hashing():
    name = "plasma mag.png"
    expected_canon = "Plasma_mag.png"
    expected_md5 = hashlib.md5(expected_canon.encode("utf-8")).hexdigest()
    a, ab, canon = file_disk_path(name)
    assert canon == expected_canon
    assert a == expected_md5[0]
    assert ab == expected_md5[:2]


# ----- cookie string parser ------------------------------------------------

def test_parse_cookies_empty():
    assert _parse_cookies(None) == {}
    assert _parse_cookies("") == {}


def test_parse_cookies_single():
    assert _parse_cookies("cf_clearance=abc123") == {"cf_clearance": "abc123"}


def test_parse_cookies_multi_with_whitespace():
    out = _parse_cookies("a=1; b=2;  c =3 ")
    assert out == {"a": "1", "b": "2", "c": "3"}


def test_parse_cookies_skips_garbage():
    out = _parse_cookies("a=1; nonsense_no_eq; b=2")
    assert out == {"a": "1", "b": "2"}


# ----- impersonate auto-detection -----------------------------------------

def test_detect_impersonate_firefox():
    ua = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:150.0) Gecko/20100101 Firefox/150.0"
    assert _detect_impersonate(ua) == "firefox135"


def test_detect_impersonate_chrome():
    ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/130.0.0.0 Safari/537.36"
    assert _detect_impersonate(ua) == "chrome"


def test_detect_impersonate_edge_takes_priority_over_chrome_substring():
    # Edge UAs include both 'Chrome' and 'Edg/'.
    ua = "Mozilla/5.0 (Windows NT 10.0) Chrome/130.0.0.0 Edg/130.0.2849.46"
    assert _detect_impersonate(ua) == "edge99"


def test_detect_impersonate_safari_only():
    ua = "Mozilla/5.0 (Macintosh) AppleWebKit/605.1.15 Version/17.0 Safari/605.1.15"
    assert _detect_impersonate(ua) == "safari15_5"


def test_detect_impersonate_unknown_falls_back():
    assert _detect_impersonate("curl/8.0") == "chrome"
    assert _detect_impersonate("") == "chrome"
    assert _detect_impersonate(None) == "chrome"
