"""Tests for the revision-policy filter.

Run with: python -m pytest tests/ -v

The filter is the load-bearing piece — the other subcommands are network
glue. Keep the filter unit-testable with synthetic inputs so the closest-
to-target / drop-after-cutoff policy is locked in.
"""
from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from scraper import (  # noqa: E402
    closest_revision, filter_export, parse_ts,
)
from xml.etree import ElementTree as ET


MW_NS = "http://www.mediawiki.org/xml/export-0.10/"


def _page(title: str, ns: int, revisions: list[tuple[str, str]]) -> ET.Element:
    """Build a synthetic <page> with the given revisions.
    revisions: list of (timestamp_iso, text)."""
    p = ET.Element(f"{{{MW_NS}}}page")
    ET.SubElement(p, f"{{{MW_NS}}}title").text = title
    ET.SubElement(p, f"{{{MW_NS}}}ns").text = str(ns)
    ET.SubElement(p, f"{{{MW_NS}}}id").text = "1"
    for ts, text in revisions:
        r = ET.SubElement(p, f"{{{MW_NS}}}revision")
        ET.SubElement(r, f"{{{MW_NS}}}id").text = str(hash(ts) & 0xFFFFFF)
        ET.SubElement(r, f"{{{MW_NS}}}timestamp").text = ts
        ET.SubElement(r, f"{{{MW_NS}}}text").text = text
    return p


def test_picks_revision_closer_to_target_before():
    """A revision on Dec 30 2020 is closer to Jan 1 2021 than Jun 1 2021."""
    target = parse_ts("2021-01-01T00:00:00Z")
    cutoff = parse_ts("2022-01-01T00:00:00Z")
    page = _page("Foo", 0, [
        ("2020-06-01T00:00:00Z", "old"),
        ("2020-12-30T00:00:00Z", "near"),
        ("2021-06-01T00:00:00Z", "after_target_but_farther"),
    ])
    chosen = closest_revision(page, target, cutoff)
    assert chosen is not None
    text = chosen.find(f"{{{MW_NS}}}text").text
    assert text == "near"


def test_picks_revision_after_target_when_closer():
    """A revision Jan 5 2021 is closer to Jan 1 2021 than Dec 1 2020."""
    target = parse_ts("2021-01-01T00:00:00Z")
    cutoff = parse_ts("2022-01-01T00:00:00Z")
    page = _page("Foo", 0, [
        ("2020-12-01T00:00:00Z", "month_before"),
        ("2021-01-05T00:00:00Z", "few_days_after"),
    ])
    chosen = closest_revision(page, target, cutoff)
    assert chosen is not None
    assert chosen.find(f"{{{MW_NS}}}text").text == "few_days_after"


def test_drops_revisions_after_cutoff():
    """Revisions after cutoff are never candidates."""
    target = parse_ts("2021-01-01T00:00:00Z")
    cutoff = parse_ts("2022-01-01T00:00:00Z")
    page = _page("Foo", 0, [
        ("2022-06-01T00:00:00Z", "after_cutoff"),
        ("2023-01-01T00:00:00Z", "way_after_cutoff"),
        ("2020-01-01T00:00:00Z", "year_before_target"),
    ])
    chosen = closest_revision(page, target, cutoff)
    assert chosen is not None
    assert chosen.find(f"{{{MW_NS}}}text").text == "year_before_target"


def test_page_with_only_revisions_after_cutoff_is_skipped():
    """Page first written in 2022+ has no candidate revision."""
    target = parse_ts("2021-01-01T00:00:00Z")
    cutoff = parse_ts("2022-01-01T00:00:00Z")
    page = _page("Foo", 0, [
        ("2022-06-01T00:00:00Z", "only_revision"),
        ("2023-01-01T00:00:00Z", "newer_revision"),
    ])
    assert closest_revision(page, target, cutoff) is None


def test_filter_export_end_to_end(tmp_path: Path):
    """Build a synthetic XML with three pages, run filter_export, verify
    the resulting file has the expected pages + one revision each."""
    src = tmp_path / "in.xml"
    dst = tmp_path / "out.xml"

    ET.register_namespace("", MW_NS)
    root = ET.Element(f"{{{MW_NS}}}mediawiki",
                      attrib={"version": "0.10"})
    si = ET.SubElement(root, f"{{{MW_NS}}}siteinfo")
    ET.SubElement(si, f"{{{MW_NS}}}sitename").text = "test"
    ET.SubElement(si, f"{{{MW_NS}}}base").text = "https://test.invalid/"
    ET.SubElement(si, f"{{{MW_NS}}}generator").text = "synthetic"
    ET.SubElement(si, f"{{{MW_NS}}}case").text = "first-letter"

    # Page A: has a revision near target — should be kept.
    root.append(_page("KeepNear", 0, [
        ("2020-06-01T00:00:00Z", "old"),
        ("2020-12-30T00:00:00Z", "near"),
        ("2025-01-01T00:00:00Z", "way_after"),
    ]))
    # Page B: only revisions after cutoff — should be dropped.
    root.append(_page("DropTooNew", 0, [
        ("2022-06-01T00:00:00Z", "after"),
        ("2023-01-01T00:00:00Z", "way_after"),
    ]))
    # Page C: closest revision is after target but before cutoff — should be kept.
    root.append(_page("KeepAfter", 10, [
        ("2021-06-01T00:00:00Z", "six_months_after"),
    ]))

    ET.ElementTree(root).write(src, encoding="utf-8", xml_declaration=True)

    target = parse_ts("2021-01-01T00:00:00Z")
    cutoff = parse_ts("2022-01-01T00:00:00Z")
    stats = filter_export(src, dst, target, cutoff)

    assert stats["pages_seen"] == 3
    assert stats["pages_kept"] == 2
    assert stats["pages_dropped_after_cutoff"] == 1

    out_root = ET.parse(dst).getroot()
    kept_titles = {
        p.find(f"{{{MW_NS}}}title").text
        for p in out_root.findall(f"{{{MW_NS}}}page")
    }
    assert kept_titles == {"KeepNear", "KeepAfter"}

    keep_near = next(
        p for p in out_root.findall(f"{{{MW_NS}}}page")
        if p.find(f"{{{MW_NS}}}title").text == "KeepNear"
    )
    revs = keep_near.findall(f"{{{MW_NS}}}revision")
    assert len(revs) == 1
    assert revs[0].find(f"{{{MW_NS}}}text").text == "near"
