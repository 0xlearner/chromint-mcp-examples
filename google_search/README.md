# Google search through chromint-mcp

Google results pages, past reCAPTCHA, paginated and parsed into JSON, with
every MCP tool call and its cost logged.

```bash
python google_search/search.py "best web scraping api" "eiffel tower" --pages 3
```

## How it works

1. `fetch` the results page through a residential exit. Google answers a
   fresh client with a reCAPTCHA page (HTTP 200). The server reports it as
   `block.status: challenged` and names the next call in `block.next`:
   `mint_session`.
2. `mint_session` runs a real Chrome on the browser worker, gets past the
   challenge (30-50 s) and returns a `session`. When it takes longer than the
   server's 45 s wait, it returns a `job` to poll with `get_job`. A mint whose
   exits are all burned fails, uncharged, and the script mints again on new
   ones (`--max-mints`).
3. Every page is then a plain `fetch(session=...)`. Google's HTML is
   0.4-1.8 MB per page and `fetch` returns up to 200,000 characters per call,
   so the rest comes in `cursor` chunks, which are free.
4. `serp.py` parses each page. Pages go `start=0, 10, 20, ...` until Google
   has no next page.
5. At the end, the script reads the run's events and platform usage back
   from Apify and compares them with its own log.

## Files

- `search.py`: the MCP client, the call and cost log, the cost check.
- `serp.py`: the parser (lxml, no network I/O). `parse(html)` returns
  organic results, the AI Overview as markdown (with the sections Google
  streams in as deferred fragments put back) and its cited URLs, People also
  ask questions, forum threads ("What people are saying"), videos, Things to
  know, related searches, the knowledge panel, ads and pagination.
  `parse_markdown(md)` reads the server's markdown instead.
- `sample_output.json`: a trimmed result file from a real run.

## Options

| Option | Default | |
|---|---|---|
| `--pages N` | 3 | results pages per query (stops early when Google has no next page) |
| `--country`, `--language` | `US`, `en` | proxy exit country, and Google's `gl=` and `hl=` |
| `--proxy` | `residential` | `datacenter` is cheaper per GB but passes Google less often |
| `--format` | `html` | `markdown`: one call per page, organic results and the first part of the AI Overview only |
| `--resolve N` | 0 | follow the `/goto` links of the top N results for their real URLs (one charged `fetch` each) |
| `--session HANDLE` | | reuse a session from an earlier run (`runs/<ts>/session.txt`; it lives 1 h after its last use) |
| `--max-mints N` | 3 | mint budget per run |
| `--save-html` | | keep each page's raw HTML |
| `--recheck RUN_DIR` | | read a finished run's costs again (Apify books proxy traffic minutes late) |
| `--verbose` | | also log the mint's progress notifications |

Output goes to `google_search/runs/<timestamp>/`: one `<query>.json` per
query, `calls.jsonl` (one line per tool call), `costs.json`,
`apify_state.json`, `run.log` and `session.txt`.

## What a run cost (verified 20 Nov 2026)

4 queries × 3 pages, US residential, on a cold server:

| | USD |
|---|---|
| 12 × `fetch` | 0.0180 |
| 1 × `mint` | 0.0250 |
| server usage (Standby run, 301 s incl. cold start and idle) | 0.0313 |
| worker usage, ready mint | 0.0433 |
| worker usage, failed mint (exits burned, no event) | 0.0297 |
| **total, 110 unique organic results** | **0.1473** |

Once a session exists, a page costs about $0.003 (the `fetch` event plus
residential proxy). A session is the expensive part: about $0.07 when the
first mint works, $0.11 on average over 9 mint jobs of which 5 failed (exits
already burned by reCAPTCHA). A failed mint charges no event but its
platform usage ($0.02-0.06) is paid, and in one test run all three mints
failed: $0.12 for no page. `--max-mints` caps that.

## Things to know

- **Result URLs aren't in the page.** Google links every result to
  `/goto?url=<opaque token>`. `breadcrumb_url` is built from the visible
  breadcrumb and matched the real URL for 4 of 9 results in a test; `url` is filled only with
  `--resolve`.
- **People also ask answers aren't in the page.** Google loads them on click,
  so the output has the questions only.
- **Measure costs on a quiet account.** The server's Standby run is shared
  by all your MCP sessions, so the cost check is exact only when nothing else
  uses the server during the run (the script warns when counts don't match).
  Its figures settle a few minutes after the run: use `--recheck`.
- **The parser will need updates.** It relies on ARIA roles, `data-*`
  attributes and block titles rather than Google's generated class names,
  and it logs the blocks it found on each page. A block that stops appearing
  in the log is the first sign that something changed.
