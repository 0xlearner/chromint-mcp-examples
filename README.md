# chromint-mcp examples

Working Python clients for [chromint-mcp](https://apify.com/nextgensda/chromint-mcp),
a remote MCP server on Apify that fetches web pages and APIs past anti-bot
walls: requests with a verified Chrome's network fingerprint, challenges
solved over HTTP or once in a real Chrome, clean markdown or the raw HTML.

Each example is a script you can run as is, with the tool calls, what they
returned and what they cost logged as it goes.

| Example | What it does |
|---|---|
| [`google_search/`](google_search/) | Google results past reCAPTCHA, paginated, parsed into JSON: organic results, AI Overview, People also ask, forums, videos, knowledge panel. Every call and its cost logged, then checked against Apify's run records. |

Write-up: [How to Scrape Google Search in 2026: AI Overviews, Pagination and the Real Cost per Page](https://nextgensda.com/writing/how-to-scrape-google-search/).

## Setup

Python 3.11 or newer.

```bash
git clone https://github.com/0xlearner/chromint-mcp-examples.git
cd chromint-mcp-examples
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # put your Apify token in it
```

You need an [Apify](https://apify.com) account and its API token (Console →
Settings → API & Integrations). The server runs in your account, so its runs
are billed to it.

## What it costs

chromint-mcp is pay per event plus platform usage:

- `fetch`: $0.0015 per page that comes back 2xx with nothing in the way.
  Challenged or blocked responses, and later `cursor` chunks of a page, are free.
- `mint`: $0.025 per browser session that is ready (charged by the
  [browser worker](https://apify.com/nextgensda/chromint-browser-worker) run).
- Platform usage on top: compute and Apify Proxy traffic (residential proxy
  is the largest part).

Those are the list prices on Free and Bronze plans; Silver pays 10% less,
Gold and up 20% less. Each example's README says what a run of it cost when
it was last verified.

## Connecting your own MCP client

The same server works from any MCP client (Claude, Cursor, an agent
framework) over Streamable HTTP:

```text
URL:     https://nextgensda--chromint-mcp.apify.actor/mcp
Header:  Authorization: Bearer <your Apify token>
```

More on the tools: [nextgensda.com/chromint-mcp](https://nextgensda.com/chromint-mcp/).

## License

MIT
