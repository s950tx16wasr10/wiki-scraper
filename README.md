# wiki-scraper

Snapshot scraper that grabs a target historical revision of every page on the
[tgstation13 wiki](https://wiki.tgstation13.org), and emits a MediaWiki XML
export that you can pipe straight into `importDump.php` on a downstream wiki.

**Default policy:**

- Pick the revision **closest to 2021-01-01** for each page.
- If a page has **no revision before 2022-01-01**, drop the page entirely.

Both dates are configurable per run.

## Why three subcommands

The tgstation13 wiki sits behind Cloudflare's "managed challenge", which
returns HTTP 403 to any client that doesn't pass a browser-side JS check.
That's the single hardest part of this problem — the revision policy is
trivially expressible once you have the XML.

So the tool is split into three commands you can mix-and-match:

| Command   | What it does                                                            | Needs network? |
|-----------|-------------------------------------------------------------------------|----------------|
| `filter`  | Apply the revision-closest-to-target policy to an existing export XML   | no             |
| `fetch`   | Pull pages + history straight from `Special:Export` (Cloudflare-walled) | yes            |
| `wayback` | Recover deleted pages via Internet Archive captures of `?action=raw`    | yes            |

The robust workflow is `manual export` → `filter` → `wayback` for the missing.

---

## Recommended workflow (Cloudflare-proof)

1. **Get a list of titles** you want. Either:
   - Use `Special:AllPages` in your browser, walking namespaces (Main, File, Template, Category, Module). Copy-paste page lists into a text file.
   - Use the wiki's category tree (`Category:Game_objects` etc.) to recursively scrape titles.
   - Skip this step and just dump everything via the wiki's full XML dump (if the operator publishes one).

2. **Export the histories**. In your browser (already authenticated past Cloudflare):
   - Open <https://wiki.tgstation13.org/Special:Export>.
   - Paste your title list into the textarea (one per line).
   - Uncheck "Include only the current revision of each page".
   - Click **Export**. Save the resulting XML.
   - For large lists you'll want to chunk into batches of ~100 titles; the export endpoint truncates beyond a point.

3. **Run the filter** to keep one revision per page, the one closest to 2021-01-01:

   ```bash
   python src/scraper.py filter \
       --input  exports/raw-full-history.xml \
       --output exports/redux-2021-01-01.xml
   ```

   Optional overrides:
   - `--target 2021-06-15T00:00:00Z` to aim for a different date
   - `--cutoff 2022-06-01T00:00:00Z` to widen the "page must exist before X" cutoff
   - Either `--input` or `--output` may end in `.gz` and is read/written gzipped.

4. **Fill in deleted pages** if you have a list of titles that were
   present in 2021 but aren't on the wiki today. Save the titles to a text
   file (one per line) and run:

   ```bash
   python src/scraper.py wayback \
       --titles missing-titles.txt \
       --output exports/redux-2021-deleted.xml
   ```

   This hits the Internet Archive CDX API for `?action=raw` captures of
   each title near the target date. Pages with no captured raw wikitext
   are reported and skipped — there's no clean way to recover wikitext
   from rendered HTML captures.

5. **Import into your wiki**:

   ```bash
   php maintenance/importDump.php --quiet < exports/redux-2021-01-01.xml
   php maintenance/importDump.php --quiet < exports/redux-2021-deleted.xml
   php maintenance/rebuildrecentchanges.php
   php maintenance/initSiteStats.php --update
   ```

---

## Optional `fetch` command (if you can clear Cloudflare programmatically)

`fetch` automates the manual browser steps via `curl_cffi`'s Chrome TLS
fingerprint. It clears most Cloudflare managed-challenge variants without
needing a real browser:

```bash
pip install curl_cffi
python src/scraper.py fetch \
    --namespaces 0,6,10,14,828 \
    --output exports/redux-raw.xml \
    --delay 0.5
```

When it works, it produces both `redux-raw.xml` (combined Special:Export
output from every title) and `redux-raw.xml.filtered.xml` (after the
revision-policy filter). When Cloudflare's challenge complexity climbs
beyond what `curl_cffi` clears, you'll get HTTP 403 on the first request
and the script will explain the manual workflow above.

---

## What the filter actually does

For each `<page>` in the input XML:

1. Collect every `<revision>` with timestamp `<= cutoff`.
2. If that set is empty → drop the page.
3. Otherwise → keep the revision whose timestamp has the smallest absolute
   distance to `target`. Drop the rest.

The output preserves `<siteinfo>` verbatim (so `importDump.php` finds the
namespace mappings and base URL) and emits exactly one `<revision>` per
kept `<page>`. The XML is wrapped under the MediaWiki export-0.10
namespace.

`filter` uses `xml.etree.ElementTree.iterparse` and clears in-tree
elements after processing, so it streams arbitrarily large dumps without
loading them whole. Tested fine on synthetic 200k-page inputs.

---

## Namespace defaults

The `fetch` command defaults to namespaces `0,6,10,14,828`:

| Namespace | Name      | Why include                                         |
|-----------|-----------|-----------------------------------------------------|
| 0         | (Main)    | The wiki articles themselves                        |
| 6         | File      | Image metadata (file binaries imported separately)  |
| 10        | Template  | Required by `{{...}}` transclusions in articles     |
| 14        | Category  | Category page text                                  |
| 828       | Module    | Scribunto Lua modules used by templates             |

Talk namespaces (`1,3,5,7,9,11,15`), User (`2`), Project (`4`), and
MediaWiki (`8`) are excluded by default. Add them with
`--namespaces 0,6,10,14,828,4` etc. if you want them.

---

## Tests

```bash
pip install pytest
python -m pytest tests/ -v
```

The filter has unit tests for: closest-revision selection (before, after,
either side of target), revisions-after-cutoff exclusion, page-too-new
drop, and a full end-to-end XML round-trip.

---

## License

MIT (see `LICENSE`). The exported content remains under the upstream
wiki's license (CC BY-SA 3.0 for tgstation13).
