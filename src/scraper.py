#!/usr/bin/env python3
"""
wiki-scraper — snapshot tool for the tgstation13 wiki at a target historical date.

Three subcommands cover the workflow:

  filter    Take a MediaWiki Special:Export XML dump (one with full history)
            and emit a new XML containing exactly one revision per page —
            the revision closest to TARGET, restricted to revisions on or
            before CUTOFF. Pages whose oldest revision is after CUTOFF are
            dropped entirely. Pure offline, no network.

  fetch     Pull pages and history straight from the wiki via Special:Export.
            Uses curl_cffi (Chrome TLS fingerprint) to clear the Cloudflare
            managed challenge that walls off the regular HTTP clients. Will
            fall back to plain requests if curl_cffi is not installed, in
            which case Cloudflare almost always 403s.

  wayback   Fill in pages that were deleted from the live wiki by hitting
            the Internet Archive Wayback Machine. Cloudflare-immune; the
            output is wikitext recovered from `?action=raw` captures where
            available.

The default policy (overridable per command):
  TARGET = 2021-01-01T00:00:00Z   — pick the revision closest to this
  CUTOFF = 2022-01-01T00:00:00Z   — drop pages with no revision before this

Output XML is wrapped in the MediaWiki export-0.10 namespace and is import-
ready via `importDump.php --quiet < output.xml` on a MediaWiki target.
"""

from __future__ import annotations

import argparse
import datetime as dt
import gzip
import io
import json
import sys
import time
from pathlib import Path
from typing import Iterable, Iterator
from urllib.parse import quote, urlencode
from xml.etree import ElementTree as ET

# Output namespace for files we generate. We emit 0.10 because importDump.php
# on MW 1.39+ reads it without complaint and 0.11 isn't universally supported
# yet across downstream wikis. Input files can be 0.10 OR 0.11 — the iter_*
# helpers below ignore the namespace when matching tags.
MW_NS = "http://www.mediawiki.org/xml/export-0.10/"


def _local(tag: str) -> str:
    """Return the local tag name with any XML namespace stripped."""
    return tag.split("}", 1)[-1] if "}" in tag else tag

DEFAULT_API = "https://wiki.tgstation13.org/api.php"
DEFAULT_EXPORT = "https://wiki.tgstation13.org/Special:Export"
DEFAULT_TARGET = "2021-01-01T00:00:00Z"
DEFAULT_CUTOFF = "2022-01-01T00:00:00Z"
DEFAULT_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)


# --- ISO timestamp helpers ----------------------------------------------------

def parse_ts(s: str) -> dt.datetime:
    """Parse a MediaWiki ISO 8601 'Z' timestamp into a tz-aware datetime."""
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    return dt.datetime.fromisoformat(s)


def fmt_ts(t: dt.datetime) -> str:
    """Reverse of parse_ts."""
    return t.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --- XML helpers (filter command) --------------------------------------------

def _q(name: str) -> str:
    """Qualified MediaWiki-export tag name. ET wants {ns}tag."""
    return f"{{{MW_NS}}}{name}"


def iter_pages(root: ET.Element) -> Iterator[ET.Element]:
    """Yield every <page> element under the export root, regardless of which
    MediaWiki XML namespace the input uses (export-0.10 vs export-0.11)."""
    for child in root:
        if _local(child.tag) == "page":
            yield child


def iter_revs(page: ET.Element) -> Iterator[ET.Element]:
    """Same namespace-agnostic walk for <revision> children."""
    for child in page:
        if _local(child.tag) == "revision":
            yield child


def get_text(el: ET.Element, name: str) -> str | None:
    """Find a direct child by local tag name, regardless of namespace."""
    for child in el:
        if _local(child.tag) == name:
            return child.text
    return None


def closest_revision(
    page: ET.Element, target: dt.datetime, cutoff: dt.datetime
) -> ET.Element | None:
    """Pick the revision element whose timestamp is closest to `target`,
    subject to timestamp <= cutoff. Returns None if no such revision exists
    on the page (i.e., the page was first written after `cutoff`)."""
    best: ET.Element | None = None
    best_distance: dt.timedelta | None = None
    for rev in iter_revs(page):
        ts_str = get_text(rev, "timestamp")
        if not ts_str:
            continue
        try:
            ts = parse_ts(ts_str)
        except Exception:
            continue
        if ts > cutoff:
            continue
        distance = abs(ts - target)
        if best_distance is None or distance < best_distance:
            best = rev
            best_distance = distance
    return best


def filter_export(
    in_path: Path, out_path: Path, target: dt.datetime, cutoff: dt.datetime,
    progress_every: int = 500,
) -> dict:
    """Read a Special:Export full-history XML at in_path, drop every revision
    that isn't the closest-to-target<=cutoff one for its page, drop pages
    with no eligible revision, write the result to out_path. Returns a stats
    dict.

    Prints a one-line progress every `progress_every` pages (set to 0 to
    silence). On a multi-hundred-MB dump the iterparse pass takes minutes;
    without the heartbeat it's indistinguishable from a hang."""
    ET.register_namespace("", MW_NS)

    src = _open_maybe_gz(in_path, "rb")
    t_start = time.time()
    try:
        # iterparse streams the document; we never hold the full tree in memory.
        context = ET.iterparse(src, events=("start", "end"))
        _, root_in = next(context)  # the wrapper <mediawiki> element
        root_out = ET.Element(root_in.tag, attrib=root_in.attrib)

        stats = {
            "pages_seen": 0,
            "pages_kept": 0,
            "pages_dropped_after_cutoff": 0,
            "pages_dropped_no_revision": 0,
            "revisions_seen": 0,
            "revisions_kept": 0,
        }

        for event, elem in context:
            if event != "end":
                continue
            tag = elem.tag
            local = tag.split("}", 1)[-1] if "}" in tag else tag

            if local == "siteinfo":
                # Copy <siteinfo> verbatim — importDump.php uses this for
                # base URL, namespace mappings, and generator metadata.
                root_out.append(elem)
                root_in.remove(elem)
                continue

            if local != "page":
                continue

            stats["pages_seen"] += 1
            revs = list(iter_revs(elem))
            stats["revisions_seen"] += len(revs)
            chosen = closest_revision(elem, target, cutoff)
            title = get_text(elem, "title") or "?"
            if chosen is None:
                if revs:
                    stats["pages_dropped_after_cutoff"] += 1
                else:
                    stats["pages_dropped_no_revision"] += 1
            else:
                for r in revs:
                    if r is not chosen:
                        elem.remove(r)
                root_out.append(_clone(elem))
                stats["pages_kept"] += 1
                stats["revisions_kept"] += 1

            if progress_every and stats["pages_seen"] % progress_every == 0:
                rate = stats["pages_seen"] / max(time.time() - t_start, 0.001)
                print(
                    f"[filter] {stats['pages_seen']:>6} pages seen "
                    f"({stats['pages_kept']} kept, "
                    f"{stats['pages_dropped_after_cutoff']} too-new) "
                    f"@ {rate:.0f} pages/s  last: {title[:60]!r}",
                    flush=True,
                )
            # Drop the page from the in-tree to keep memory flat.
            root_in.clear()

        elapsed = time.time() - t_start
        print(
            f"[filter] done in {elapsed:.1f}s: "
            f"{stats['pages_seen']} pages seen, "
            f"{stats['pages_kept']} kept, "
            f"{stats['revisions_seen']} revisions seen, "
            f"{stats['revisions_kept']} kept",
            flush=True,
        )

        tree = ET.ElementTree(root_out)
        print(f"[filter] writing {out_path}", flush=True)
        with _open_maybe_gz(out_path, "wb") as f:
            tree.write(f, encoding="utf-8", xml_declaration=True)
        print(
            f"[filter] wrote {out_path} ({out_path.stat().st_size:,} bytes)",
            flush=True,
        )
        return stats
    finally:
        src.close()


def _clone(elem: ET.Element) -> ET.Element:
    """Deep-copy an Element. ET doesn't expose a copy() with a public API
    that's reliable across versions; round-trip through tostring/fromstring."""
    return ET.fromstring(ET.tostring(elem))


def _open_maybe_gz(path: Path, mode: str):
    """Open a file transparently, gzip if the name ends with .gz."""
    if str(path).endswith(".gz"):
        return gzip.open(path, mode)
    return open(path, mode)


# --- Live fetcher (via Special:Export) ---------------------------------------

def _http_session(user_agent: str):
    """Return a session-like object. Prefer curl_cffi.Session impersonating
    Chrome (the only realistic way past Cloudflare). Fall back to requests
    with a stern UA; the user will likely 403 but they'll see why."""
    try:
        from curl_cffi import requests as cffi_requests  # type: ignore
        sess = cffi_requests.Session(impersonate="chrome")
        sess.headers["User-Agent"] = user_agent
        return ("curl_cffi", sess)
    except ImportError:
        import requests  # type: ignore
        sess = requests.Session()
        sess.headers["User-Agent"] = user_agent
        return ("requests", sess)


def list_all_pages(api_url: str, namespace: int, user_agent: str, delay: float) -> Iterator[dict]:
    """Stream every page in the given namespace via the regular MediaWiki
    JSON API. Yields {pageid, ns, title}."""
    backend, sess = _http_session(user_agent)
    cont: dict = {}
    while True:
        params = {
            "action": "query",
            "format": "json",
            "list": "allpages",
            "apnamespace": str(namespace),
            "aplimit": "max",
            **cont,
        }
        url = f"{api_url}?{urlencode(params)}"
        r = sess.get(url, timeout=30)
        if r.status_code != 200:
            raise RuntimeError(
                f"allpages {namespace} returned HTTP {r.status_code} "
                f"({backend}). Cloudflare likely blocking; try the manual "
                f"Special:Export workflow described in README."
            )
        data = r.json()
        for p in data.get("query", {}).get("allpages", []):
            yield p
        if "continue" in data:
            cont = data["continue"]
        else:
            break
        time.sleep(delay)


def export_one_history(
    export_url: str, title: str, cutoff_iso: str, user_agent: str, delay: float
) -> bytes:
    """Hit Special:Export for a single title with history=1, capped at
    revisions <= cutoff (offset=CUTOFF, dir=desc). Returns raw XML bytes."""
    backend, sess = _http_session(user_agent)
    params = {
        "pages": title,
        "history": "1",
        "offset": cutoff_iso,
        "dir": "desc",
        "limit": "5000",
        "action": "submit",
    }
    r = sess.post(export_url, data=params, timeout=60)
    if r.status_code != 200:
        raise RuntimeError(
            f"Special:Export {title!r} returned HTTP {r.status_code} ({backend})."
        )
    time.sleep(delay)
    return r.content


# --- Wayback fallback for deleted pages --------------------------------------

WAYBACK_CDX = "https://web.archive.org/cdx/search/cdx"
WAYBACK_PREFIX = "https://web.archive.org/web"


def wayback_closest(url: str, target_yyyymmdd: str, user_agent: str) -> dict | None:
    """Find the Wayback snapshot closest to target_yyyymmdd for the given
    upstream URL. Returns {timestamp, original, statuscode} or None."""
    backend, sess = _http_session(user_agent)
    params = {
        "url": url,
        "from": "20200101",
        "to": "20220101",
        "output": "json",
        "filter": "statuscode:200",
        "limit": "200",
    }
    r = sess.get(f"{WAYBACK_CDX}?{urlencode(params)}", timeout=30)
    if r.status_code != 200:
        return None
    rows = r.json()
    if not rows or len(rows) < 2:
        return None
    header = rows[0]
    target_dt = dt.datetime.strptime(target_yyyymmdd, "%Y%m%d").replace(tzinfo=dt.timezone.utc)
    best = None
    best_distance = None
    for row in rows[1:]:
        rec = dict(zip(header, row))
        ts = dt.datetime.strptime(rec["timestamp"], "%Y%m%d%H%M%S").replace(tzinfo=dt.timezone.utc)
        distance = abs(ts - target_dt)
        if best_distance is None or distance < best_distance:
            best = rec
            best_distance = distance
    return best


def wayback_fetch_raw(title: str, target_yyyymmdd: str, user_agent: str, base: str) -> bytes | None:
    """Look for a Wayback capture of `index.php?title=X&action=raw` (raw
    wikitext) closest to target. Falls back to no capture if none exists.
    """
    # Wayback URL-matches with prefix matching, so we ask for both action=raw
    # and action=edit captures.
    candidates = [
        f"{base}/index.php?title={quote(title)}&action=raw",
        f"{base}/index.php?action=raw&title={quote(title)}",
    ]
    backend, sess = _http_session(user_agent)
    for url in candidates:
        snap = wayback_closest(url, target_yyyymmdd, user_agent)
        if snap is None:
            continue
        archived = f"{WAYBACK_PREFIX}/{snap['timestamp']}id_/{snap['original']}"
        r = sess.get(archived, timeout=60)
        if r.status_code == 200 and r.content:
            return r.content
    return None


# --- subcommands -------------------------------------------------------------

def cmd_filter(args: argparse.Namespace) -> int:
    target = parse_ts(args.target)
    cutoff = parse_ts(args.cutoff)
    in_path = Path(args.input)
    out_path = Path(args.output)
    if not in_path.exists():
        print(f"error: input file not found: {in_path}", file=sys.stderr)
        return 2
    print(f"Filtering {in_path} -> {out_path}")
    print(f"  target = {fmt_ts(target)}")
    print(f"  cutoff = {fmt_ts(cutoff)}")
    stats = filter_export(in_path, out_path, target, cutoff)
    for k, v in stats.items():
        print(f"  {k}: {v}")
    return 0


def cmd_fetch(args: argparse.Namespace) -> int:
    namespaces = [int(n) for n in args.namespaces.split(",")]
    out_path = Path(args.output)
    cutoff = parse_ts(args.cutoff)
    cutoff_iso = fmt_ts(cutoff)
    target = parse_ts(args.target)

    backend, _sess = _http_session(args.user_agent)
    print(f"HTTP backend: {backend}")
    if backend != "curl_cffi":
        print(
            "WARNING: curl_cffi is not installed. Cloudflare will almost "
            "certainly block this. Run `pip install curl_cffi` first, or "
            "use the manual browser export workflow (see README).",
            file=sys.stderr,
        )

    # Collect titles per namespace
    all_titles: list[tuple[int, str]] = []
    for ns in namespaces:
        print(f"\nNamespace {ns}: listing pages")
        try:
            for page in list_all_pages(args.api, ns, args.user_agent, args.delay):
                all_titles.append((ns, page["title"]))
                if args.limit and len(all_titles) >= args.limit:
                    break
        except RuntimeError as exc:
            print(f"  FAILED: {exc}", file=sys.stderr)
            if not all_titles:
                return 3
        if args.limit and len(all_titles) >= args.limit:
            break

    print(f"\nGot {len(all_titles)} titles. Fetching histories <= {cutoff_iso}...")
    out_path.parent.mkdir(parents=True, exist_ok=True)

    # Write a combined wrapper around per-page exports. Special:Export emits
    # complete <mediawiki>...</mediawiki> documents; we strip outer wrapper
    # and concatenate <page> elements, then re-wrap.
    siteinfo_xml: bytes | None = None
    page_chunks: list[bytes] = []
    for ns, title in all_titles:
        print(f"  [{ns}] {title}", end=" ... ")
        sys.stdout.flush()
        try:
            xml_bytes = export_one_history(
                args.export, title, cutoff_iso, args.user_agent, args.delay
            )
        except RuntimeError as exc:
            print(f"FAIL ({exc})")
            continue
        try:
            doc = ET.fromstring(xml_bytes)
        except ET.ParseError as exc:
            print(f"PARSE FAIL ({exc})")
            continue
        if siteinfo_xml is None:
            si = doc.find(_q("siteinfo"))
            if si is not None:
                siteinfo_xml = ET.tostring(si)
        kept = 0
        for page_el in doc.findall(_q("page")):
            page_chunks.append(ET.tostring(page_el))
            kept += 1
        print(f"{kept} page(s)")

    print(f"\nWriting {out_path}")
    with _open_maybe_gz(out_path, "wb") as f:
        f.write(
            b'<?xml version="1.0" encoding="utf-8"?>\n'
            b'<mediawiki xmlns="' + MW_NS.encode() + b'" version="0.10" xml:lang="en">\n'
        )
        if siteinfo_xml:
            f.write(siteinfo_xml)
            f.write(b"\n")
        for chunk in page_chunks:
            f.write(chunk)
            f.write(b"\n")
        f.write(b"</mediawiki>\n")

    # Run the filter pass over the combined output.
    filtered_path = out_path.with_suffix(out_path.suffix + ".filtered.xml")
    print(f"\nApplying revision-closest-to-{fmt_ts(target)} filter -> {filtered_path}")
    stats = filter_export(out_path, filtered_path, target, cutoff)
    for k, v in stats.items():
        print(f"  {k}: {v}")
    return 0


def cmd_wayback(args: argparse.Namespace) -> int:
    target = parse_ts(args.target)
    target_yyyymmdd = target.strftime("%Y%m%d")
    out_path = Path(args.output)
    titles = [t.strip() for t in Path(args.titles).read_text().splitlines() if t.strip()]
    print(f"Looking up {len(titles)} titles on Wayback Machine, target={target_yyyymmdd}")

    # Build a minimal export wrapper. Wayback only gives wikitext, no
    # revision IDs or contributor info, so the <revision> blocks we emit
    # are intentionally sparse but importDump-friendly.
    root = ET.Element(_q("mediawiki"), attrib={
        "version": "0.10",
        "{http://www.w3.org/XML/1998/namespace}lang": "en",
    })
    si = ET.SubElement(root, _q("siteinfo"))
    ET.SubElement(si, _q("sitename")).text = "tgstation13"
    ET.SubElement(si, _q("base")).text = args.wiki_base
    ET.SubElement(si, _q("generator")).text = "wiki-scraper:wayback"
    ET.SubElement(si, _q("case")).text = "first-letter"

    kept = 0
    skipped = 0
    for title in titles:
        print(f"  {title}", end=" ... ")
        sys.stdout.flush()
        raw = wayback_fetch_raw(title, target_yyyymmdd, args.user_agent, args.wiki_base)
        if raw is None:
            print("no capture")
            skipped += 1
            continue
        page_el = ET.SubElement(root, _q("page"))
        ET.SubElement(page_el, _q("title")).text = title
        ET.SubElement(page_el, _q("ns")).text = "0"
        rev_el = ET.SubElement(page_el, _q("revision"))
        ET.SubElement(rev_el, _q("timestamp")).text = fmt_ts(target)
        ET.SubElement(rev_el, _q("model")).text = "wikitext"
        ET.SubElement(rev_el, _q("format")).text = "text/x-wiki"
        text_el = ET.SubElement(rev_el, _q("text"))
        text_el.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
        text_el.text = raw.decode("utf-8", errors="replace")
        print(f"got {len(raw)} bytes")
        kept += 1

    ET.ElementTree(root).write(out_path, encoding="utf-8", xml_declaration=True)
    print(f"\nWrote {out_path}: kept={kept}, skipped={skipped}")
    return 0


# --- argparse setup ----------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="wiki-scraper", description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    f = sub.add_parser("filter", help="Apply the revision-policy filter to an existing Special:Export XML.")
    f.add_argument("--input", required=True, help="Path to full-history Special:Export XML (.xml or .xml.gz).")
    f.add_argument("--output", required=True, help="Path to write filtered XML to (.xml or .xml.gz).")
    f.add_argument("--target", default=DEFAULT_TARGET, help=f"ISO 8601 target date (default {DEFAULT_TARGET}).")
    f.add_argument("--cutoff", default=DEFAULT_CUTOFF, help=f"ISO 8601 cutoff (drop pages whose oldest revision is after this; default {DEFAULT_CUTOFF}).")
    f.set_defaults(func=cmd_filter)

    fe = sub.add_parser("fetch", help="Pull pages + history from the wiki via Special:Export (needs curl_cffi to clear Cloudflare).")
    fe.add_argument("--api", default=DEFAULT_API, help=f"MediaWiki api.php URL (default {DEFAULT_API}).")
    fe.add_argument("--export", default=DEFAULT_EXPORT, help=f"Special:Export URL (default {DEFAULT_EXPORT}).")
    fe.add_argument("--namespaces", default="0,6,10,14,828", help="Comma-separated namespace numbers (default 0,6,10,14,828 = main, file, template, category, module).")
    fe.add_argument("--output", required=True, help="Output combined XML path.")
    fe.add_argument("--target", default=DEFAULT_TARGET)
    fe.add_argument("--cutoff", default=DEFAULT_CUTOFF)
    fe.add_argument("--user-agent", default=DEFAULT_UA)
    fe.add_argument("--delay", type=float, default=0.5, help="Seconds between requests (default 0.5).")
    fe.add_argument("--limit", type=int, default=0, help="Stop after N titles. 0 = unlimited.")
    fe.set_defaults(func=cmd_fetch)

    w = sub.add_parser("wayback", help="Recover wikitext for deleted pages via Wayback Machine ?action=raw captures.")
    w.add_argument("--titles", required=True, help="Path to a newline-delimited file of titles to look up.")
    w.add_argument("--output", required=True, help="Path to write recovered XML to.")
    w.add_argument("--target", default=DEFAULT_TARGET)
    w.add_argument("--wiki-base", default="https://wiki.tgstation13.org", help="Wiki base URL prefix (no trailing slash).")
    w.add_argument("--user-agent", default=DEFAULT_UA)
    w.set_defaults(func=cmd_wayback)

    return ap


def main(argv: list[str] | None = None) -> int:
    ap = build_parser()
    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
