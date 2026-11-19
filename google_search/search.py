"""Google search through the chromint MCP server on Apify, end to end.

For each query: fetch the results page, follow what the server says to do
next (`block.next`: Google answers a fresh client with reCAPTCHA, so the
first step is a `mint_session` in a real Chrome on the browser worker),
fetch every page on that session, read each page's HTML in `cursor` chunks
(free), parse it (`serp.py`) and write one JSON file per query. Every tool
call is logged with its time, outcome and price; at the end the run's real
costs are read back from Apify (events counted, platform usage) and set
against the log.

    cp .env.example .env              # then put your APIFY_TOKEN in it
    python google_search/search.py "best web scraping api" --pages 3

Output goes to google_search/runs/<timestamp>/ (git-ignored):
`<query>.json`, `calls.jsonl` (one line per tool call), `costs.json`,
`apify_state.json` and `run.log`.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from apify_client import ApifyClientAsync
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport
from loguru import logger

sys.path.insert(0, str(Path(__file__).parent))
import serp  # noqa: E402

SERVER_URL = "https://nextgensda--chromint-mcp.apify.actor/mcp"
SERVER_ACTOR = "nextgensda/chromint-mcp"
WORKER_ACTOR = "nextgensda/chromint-browser-worker"
#: List prices per event (USD), read back from the runs at the end; these
#: are only for the live log. Free and Bronze plans pay these, Silver -10%,
#: Gold and up -20%.
PRICES = {"fetch": 0.0015, "mint": 0.025, "browse": 0.015}
#: Characters per `fetch` call; Google's results pages are 0.4-2 MB of HTML.
CHUNK = 200_000
HERE = Path(__file__).parent

logger.level("COST", no=25, color="<yellow><bold>", icon="$")


# --- the call log -----------------------------------------------------------


@dataclass
class Call:
    seq: int
    tool: str
    arguments: dict[str, Any]
    started: str
    seconds: float = 0.0
    ok: bool = True
    error: str | None = None
    status: int | str | None = None
    block: str | None = None
    vendor: str | None = None
    cached: bool | None = None
    chars: int | None = None
    total_chars: int | None = None
    #: The pay-per-event event this call triggers (by the server's rules) and
    #: its list price; `None` = free.
    event: str | None = None
    price: float = 0.0
    note: str | None = None


@dataclass
class Ledger:
    calls: list[Call] = field(default_factory=list)
    jobs: list[str] = field(default_factory=list)  # worker run IDs (mints)
    charged_jobs: list[str] = field(default_factory=list)
    path: Path | None = None

    def add(self, call: Call) -> None:
        self.calls.append(call)
        if self.path:
            with self.path.open("a") as out:
                out.write(json.dumps(asdict(call), ensure_ascii=False) + "\n")

    def event_cost(self) -> float:
        return sum(call.price for call in self.calls)


def _short(handle: str | None) -> str | None:
    """Session handles are only usable with your token, but keep logs terse."""
    return f"{handle[:10]}…" if handle else None


def _shown(arguments: dict[str, Any]) -> dict[str, Any]:
    shown = dict(arguments)
    if "session" in shown:
        shown["session"] = _short(shown["session"])
    if isinstance(shown.get("url"), str) and len(shown["url"]) > 140:
        shown["url"] = shown["url"][:140] + "…"
    return shown


class Server:
    """The MCP client, with every call logged and priced."""

    def __init__(self, client: Client, ledger: Ledger) -> None:
        self.client = client
        self.ledger = ledger

    async def call(
        self, tool: str, arguments: dict[str, Any], note: str | None = None
    ) -> dict[str, Any] | None:
        seq = len(self.ledger.calls) + 1
        call = Call(
            seq, tool, _shown(arguments), datetime.now(UTC).isoformat(timespec="seconds"), note=note
        )
        logger.info("→ #{} {} {}", seq, tool, json.dumps(call.arguments, ensure_ascii=False))

        async def progress(done: float, total: float | None, message: str | None) -> None:
            logger.debug("  … #{} {} {:.0f}/{:.0f}s {}", seq, tool, done, total or 0, message or "")

        started = time.monotonic()
        try:
            result = await self.client.call_tool(
                tool, arguments, raise_on_error=False, progress_handler=progress
            )
        except Exception as error:  # transport errors, timeouts
            call.seconds = round(time.monotonic() - started, 2)
            call.ok, call.error = False, f"{type(error).__name__}: {error}"
            self.ledger.add(call)
            logger.error("✗ #{} {} failed after {}s: {}", seq, tool, call.seconds, call.error)
            return None
        call.seconds = round(time.monotonic() - started, 2)
        if result.is_error:
            call.ok = False
            call.error = " ".join(getattr(c, "text", "") for c in result.content or [])[:500]
            self.ledger.add(call)
            logger.error("✗ #{} {} tool error after {}s: {}", seq, tool, call.seconds, call.error)
            return None
        data = result.structured_content or {}
        self._account(call, tool, data)
        self.ledger.add(call)
        return data

    def _account(self, call: Call, tool: str, data: dict[str, Any]) -> None:
        call.status = data.get("status")
        block = data.get("block") or {}
        call.block, call.vendor = block.get("status"), block.get("vendor")
        call.cached = data.get("cached")
        call.chars = len(data.get("content") or "") or None
        call.total_chars = data.get("total_chars")
        if tool == "fetch":
            # The server charges `fetch` for a 2xx with a clean block that
            # wasn't served from its page cache (cursor chunks are free).
            if data.get("ok") and call.block == "clean" and not call.cached:
                call.event, call.price = "fetch", PRICES["fetch"]
        elif tool in ("mint_session", "get_job", "browse") and data.get("job"):
            job = data["job"]
            if job not in self.ledger.jobs:
                self.ledger.jobs.append(job)  # every worker run costs platform usage
            # The worker run charges its event (`mint`) once, when the job is ready.
            if data.get("status") == "ready" and job not in self.ledger.charged_jobs:
                self.ledger.charged_jobs.append(job)
                kind = data.get("kind", "mint")
                call.event, call.price = kind, PRICES[kind]
        if data.get("status") == "failed" or call.block not in (None, "clean"):
            level = "WARNING"
        elif data.get("status") == "running":
            level = "INFO"
        else:
            level = "SUCCESS"
        summary = {
            "status": call.status,
            "block": call.block,
            "vendor": call.vendor,
            "cached": call.cached,
            "chars": call.chars,
            "total_chars": call.total_chars,
            "session": _short(data.get("session")),
            "job": data.get("job"),
        }
        summary = {k: v for k, v in summary.items() if v is not None}
        logger.log(level, "← #{} {} {}s {}", call.seq, tool, call.seconds, json.dumps(summary))
        if block.get("status") not in (None, "clean"):
            logger.warning(
                "  block: {} vendor={} reason={} next={} advice={}",
                block.get("status"),
                block.get("vendor"),
                block.get("reason"),
                (block.get("next") or {}).get("tool"),
                block.get("advice"),
            )
        if call.event:
            logger.log(
                "COST",
                "  #{} charged event `{}` ${:.4f} (running total ${:.4f})",
                call.seq,
                call.event,
                call.price,
                self.ledger.event_cost() + call.price,
            )
        elif tool == "fetch":
            why = (
                "cursor chunk from the page cache"
                if call.cached
                else f"not charged ({call.block}, HTTP {call.status})"
            )
            logger.log("COST", "  #{} free: {}", call.seq, why)


# --- the flow ---------------------------------------------------------------


@dataclass
class Exit:
    proxy: str
    country: str | None
    session: str | None = None
    mints: int = 0


class Searcher:
    def __init__(self, server: Server, exit: Exit, max_mints: int) -> None:
        self.server = server
        self.exit = exit
        self.max_mints = max_mints

    async def mint(self, arguments: dict[str, Any]) -> bool:
        """mint_session as block.next names it; a failed mint is tried again
        (each worker run starts on a new sticky proxy session, so a new IP)
        while the mint budget lasts."""
        while True:
            if self.exit.mints >= self.max_mints:
                logger.error("mint budget used up ({} mints); stopping", self.exit.mints)
                return False
            self.exit.mints += 1
            logger.info(
                "Google wants a real browser: minting a session (mint {} of at most {})",
                self.exit.mints,
                self.max_mints,
            )
            result = await self.server.call("mint_session", arguments, note="block.next")
            while result and result.get("status") == "running":
                await asyncio.sleep(5)
                result = await self.server.call("get_job", {"job": result["job"]}, note="poll")
            if result and result.get("status") == "ready" and result.get("session"):
                break
            result = result or {}
            logger.warning(
                "mint failed: {} (worker attempts: {})",
                result.get("error"),
                [(a.get("outcome"), a.get("seconds")) for a in result.get("attempts") or []],
            )
        self.exit.session = result["session"]
        logger.success(
            "session {} ready in {}s ({} attempt(s), Chrome {}), title {!r}",
            _short(self.exit.session),
            result.get("seconds"),
            len(result.get("attempts") or []),
            (result.get("identity") or {}).get("chrome")
            if isinstance(result.get("identity"), dict)
            else result.get("identity"),
            result.get("title"),
        )
        return True

    async def page(self, url: str, fmt: str) -> tuple[dict[str, Any], str] | None:
        """The whole page (every cursor chunk), following `block.next`."""
        for attempt in range(1, 5):
            arguments: dict[str, Any] = {"url": url, "format": fmt, "max_chars": CHUNK}
            if self.exit.session:
                arguments["session"] = self.exit.session
            else:
                arguments.update(proxy=self.exit.proxy, country=self.exit.country)
            first = await self.server.call("fetch", arguments, note=f"attempt {attempt}")
            if first is None:
                await asyncio.sleep(3)
                continue
            block = first["block"]
            if block["status"] == "clean" and first.get("ok"):
                parts, result = [first["content"]], first
                while result.get("next_cursor"):
                    result = await self.server.call(
                        "fetch", {**arguments, "cursor": result["next_cursor"]}, note="chunk"
                    )
                    if result is None:
                        logger.error("lost a chunk of {}; the page is incomplete", url)
                        break
                    parts.append(result["content"])
                return first, "".join(parts)
            following = block.get("next") or {}
            tool, next_args = following.get("tool"), following.get("arguments") or {}
            if tool == "mint_session":
                if not await self.mint(next_args):
                    return None
            elif tool == "drop_session":
                await self.server.call("drop_session", next_args, note="block.next")
                self.exit.session = None
            elif tool == "fetch":
                self.exit.proxy = next_args.get("proxy", self.exit.proxy)
            else:
                logger.error(
                    "{} is {} and the server has no next step; giving up on it",
                    url,
                    block["status"],
                )
                return None
        logger.error("no clean copy of {} after 4 attempts", url)
        return None

    async def resolve(self, link: str) -> dict[str, Any]:
        """Follow a /goto link on the session: the site's real URL (a fetch
        of the site itself, charged when it answers 2xx clean)."""
        result = await self.server.call(
            "fetch",
            {"url": link, "session": self.exit.session, "format": "text", "max_chars": 500},
            note="resolve",
        )
        if not result:
            return {"url": None}
        return {
            "url": result.get("url"),
            "status": result.get("status"),
            "block": result["block"]["status"],
        }


def search_url(query: str, page: int, country: str | None, language: str) -> str:
    params = {"q": query, "hl": language}
    if country:
        params["gl"] = country.lower()
    if page > 1:
        params["start"] = (page - 1) * 10
    return "https://www.google.com/search?" + urlencode(params)


async def run_query(
    searcher: Searcher, query: str, args: argparse.Namespace, out_dir: Path
) -> dict[str, Any]:
    ledger = searcher.server.ledger
    first_call = len(ledger.calls)
    started = time.monotonic()
    pages: list[dict[str, Any]] = []
    organic: list[dict[str, Any]] = []
    seen: dict[tuple[str, str | None], int] = {}
    merged: dict[str, Any] = {
        "ai_overview": None,
        "knowledge_panel": None,
        "people_also_ask": [],
        "discussions": [],
        "videos": [],
        "things_to_know": [],
        "related_searches": [],
        "ads": [],
    }
    for number in range(1, args.pages + 1):
        url = search_url(query, number, args.country, args.language)
        logger.info("── {!r} page {} ──", query, number)
        got = await searcher.page(url, args.format)
        if got is None:
            pages.append({"page": number, "url": url, "ok": False})
            break
        first, body = got
        if args.save_html:
            (
                out_dir / f"{slug(query)}-p{number}.{'html' if args.format == 'html' else 'md'}"
            ).write_text(body)
        parsed = serp.parse(body) if args.format == "html" else serp.parse_markdown(body)
        found = parsed["organic"]
        for result in found:
            key = (result["title"], result.get("breadcrumb_url") or result.get("displayed_url"))
            result["page"] = number
            result["rank"] = len(organic) + 1
            if key in seen:
                result["duplicate_of_rank"] = seen[key]
            else:
                seen[key] = result["rank"]
            organic.append(result)
        info = {
            "page": number,
            "url": url,
            "ok": True,
            "http_status": first["status"],
            "chars": len(body),
            "chunks": -(-len(body) // CHUNK),
            "organic": len(found),
            "features": parsed.get("features"),
            "parser": parsed.get("parser"),
        }
        pages.append(info)
        logger.success(
            "page {}: {} organic results, {:,} chars in {} chunk(s), blocks: {}",
            number,
            len(found),
            len(body),
            info["chunks"],
            ", ".join(parsed.get("features") or []) or "-",
        )
        if args.format == "html":
            for key in ("people_also_ask", "videos", "things_to_know", "related_searches", "ads"):
                merged[key] += parsed[key]
            if parsed["discussions"]:
                merged["discussions"].append({"page": number, **parsed["discussions"]})
            merged["ai_overview"] = merged["ai_overview"] or parsed["ai_overview"]
            merged["knowledge_panel"] = merged["knowledge_panel"] or parsed["knowledge_panel"]
            if parsed["ai_overview"]:
                ao = parsed["ai_overview"]
                logger.info(
                    "  AI Overview: {:,} chars, {} headings, {} cited URLs",
                    ao["chars"],
                    len(ao["headings"]),
                    len(ao["cited_urls"]),
                )
            if parsed["ai_overview"] is None and number == 1:
                logger.warning("  no AI Overview on page 1 for {!r}", query)
            if not parsed["pagination"]["has_next"]:
                logger.info("  no next page: stopping")
                break
        else:
            merged["ai_overview"] = merged["ai_overview"] or parsed["ai_overview"]
        if not found:
            logger.warning("  no organic results on page {}: stopping", number)
            break
    if args.resolve and organic:
        logger.info("resolving the real URLs of the top {} results (one fetch each)", args.resolve)
        for result in [r for r in organic if "duplicate_of_rank" not in r][: args.resolve]:
            if result.get("google_link"):
                resolved = await searcher.resolve(result["google_link"])
                result["url"] = resolved["url"]
                result["resolved"] = resolved
    if args.format == "html":
        merged["people_also_ask"] = list(dict.fromkeys(merged["people_also_ask"]))
    calls = ledger.calls[first_call:]
    doc = {
        "query": query,
        "fetched_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "params": {
            "pages": args.pages,
            "country": args.country,
            "language": args.language,
            "proxy": searcher.exit.proxy,
            "format": args.format,
        },
        "pages": pages,
        "organic": organic,
        **(merged if args.format == "html" else {"ai_overview_markdown": merged["ai_overview"]}),
        "stats": {
            "seconds": round(time.monotonic() - started, 1),
            "tool_calls": len(calls),
            "events": {
                e: sum(1 for c in calls if c.event == e)
                for e in PRICES
                if any(c.event == e for c in calls)
            },
            "event_cost_usd": round(sum(c.price for c in calls), 5),
            "unique_organic": sum(1 for r in organic if "duplicate_of_rank" not in r),
        },
    }
    path = out_dir / f"{slug(query)}.json"
    path.write_text(json.dumps(doc, indent=2, ensure_ascii=False))
    logger.success(
        "{!r}: {} organic ({} unique) over {} page(s), {} tool calls, ${:.4f} in events, {}s → {}",
        query,
        len(organic),
        doc["stats"]["unique_organic"],
        len([p for p in pages if p.get("ok")]),
        len(calls),
        doc["stats"]["event_cost_usd"],
        doc["stats"]["seconds"],
        path.name,
    )
    return doc


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60] or "query"


# --- what Apify says it cost ------------------------------------------------


def _run(model: Any) -> dict[str, Any]:
    return (
        model.model_dump(by_alias=True, mode="json")
        if hasattr(model, "model_dump")
        else dict(model)
    )


async def standby_runs(apify: ApifyClientAsync) -> dict[str, dict[str, Any]]:
    """The server's Standby runs in your account now (one serves every MCP
    session; Apify starts it on the first request and stops it when idle)."""
    listed = await apify.actor(SERVER_ACTOR).runs().list(desc=True, limit=10, status="RUNNING")
    runs = {}
    for item in listed.items:
        run = _run(await apify.run(_run(item)["id"]).get())
        if (run.get("meta") or {}).get("origin") == "STANDBY":
            runs[run["id"]] = run
    return runs


def _snapshot(run: dict[str, Any]) -> dict[str, Any]:
    return {
        "fetch": (run.get("chargedEventCounts") or {}).get("fetch", 0),
        "usage_usd": run.get("usageTotalUsd") or 0.0,
        "usage": run.get("usageUsd") or {},
    }


def _event_price(run: dict[str, Any], event: str) -> float:
    events = ((run.get("pricingInfo") or {}).get("pricingPerEvent") or {}).get(
        "actorChargeEvents"
    ) or {}
    price = (events.get(event) or {}).get("eventPriceUsd")
    return float(price) if price is not None else PRICES[event]


async def reconcile(
    apify: ApifyClientAsync, state: dict[str, Any], logged: list[Call]
) -> dict[str, Any]:
    """What Apify recorded for this run: `fetch` events and platform usage
    on the server's Standby run(s) since the start (`state["before"]`), and
    the worker runs (`state["jobs"]`) in full. Apify books proxy traffic a
    few minutes late, so read this again later with --recheck."""
    before, end = state["before"], state.get("after") or {}
    server: dict[str, Any] = {"fetch_events": 0, "usage_usd": 0.0, "usage_by_item": {}, "runs": []}
    fetch_price = PRICES["fetch"]
    for run_id in state["server_runs"]:
        run = _run(await apify.run(run_id).get())
        now, then = _snapshot(run), before.get(run_id) or _snapshot({})
        basis = "now"
        if run_id in end and now["fetch"] != end[run_id]["fetch"]:
            # The Standby run served other calls after ours: its later usage
            # can't be split, so stay with the figures read when we finished.
            now, basis = end[run_id], "at the end of our calls (the run served others later)"
        elif run.get("status") != "RUNNING":
            basis = "final (the Standby run has stopped: cold start and idle time included)"
        fetches = now["fetch"] - then["fetch"]
        usage = now["usage_usd"] - then["usage_usd"]
        server["fetch_events"] += fetches
        server["usage_usd"] += usage
        fetch_price = _event_price(run, "fetch")
        for item, value in now["usage"].items():
            delta = (value or 0) - (then["usage"].get(item) or 0)
            if delta > 1e-7:
                server["usage_by_item"][item] = server["usage_by_item"].get(item, 0) + delta
        server["runs"].append(
            {
                "id": run_id,
                "status": run.get("status"),
                "started_by_us": run_id not in before,
                "figures": basis,
                "fetch_events": fetches,
                "usage_usd": round(usage, 5),
            }
        )
    workers = []
    for job in state["jobs"]:
        run = _run(await apify.run(job).get())
        workers.append(
            {
                "id": job,
                "status": run.get("status"),
                "seconds": (run.get("stats") or {}).get("runTimeSecs"),
                "events": run.get("chargedEventCounts"),
                "mint_price": _event_price(run, "mint"),
                "usage_usd": run.get("usageTotalUsd") or 0.0,
                "usage_by_item": {k: v for k, v in (run.get("usageUsd") or {}).items() if v},
            }
        )
    mints = sum((w["events"] or {}).get("mint", 0) for w in workers)
    events_usd = server["fetch_events"] * fetch_price + sum(
        (w["events"] or {}).get("mint", 0) * w["mint_price"] for w in workers
    )
    usage_usd = server["usage_usd"] + sum(w["usage_usd"] for w in workers)
    logged_events: dict[str, int] = {}
    for call in logged:
        if call.event:
            logged_events[call.event] = logged_events.get(call.event, 0) + 1
    return {
        "checked_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "logged_events": logged_events,
        "logged_event_usd": round(sum(c.price for c in logged), 5),
        "apify_fetch_events": server["fetch_events"],
        "apify_mint_events": mints,
        "events_usd": round(events_usd, 5),
        "server_usage_usd": round(server["usage_usd"], 5),
        "server_usage_by_item": {k: round(v, 6) for k, v in server["usage_by_item"].items()},
        "server_runs": server["runs"],
        "workers": workers,
        "worker_usage_usd": round(sum(w["usage_usd"] for w in workers), 5),
        "usage_usd": round(usage_usd, 5),
        "total_usd": round(events_usd + usage_usd, 5),
    }


def report(costs: dict[str, Any], pages: int, organic: int) -> None:
    logger.log(
        "COST",
        "events: logged {} = ${:.4f}; Apify counted fetch={} mint={} = ${:.4f}",
        costs["logged_events"],
        costs["logged_event_usd"],
        costs["apify_fetch_events"],
        costs["apify_mint_events"],
        costs["events_usd"],
    )
    for run in costs["server_runs"]:
        logger.log(
            "COST",
            "platform usage: server run {} ({}{}) ${:.4f}, figures {}",
            run["id"],
            run["status"],
            ", started by this run" if run["started_by_us"] else "",
            run["usage_usd"],
            run["figures"],
        )
    logger.log("COST", "  server usage by item: {}", costs["server_usage_by_item"])
    for worker in costs["workers"]:
        logger.log(
            "COST",
            "platform usage: worker run {} ({}, {}s, events {}) ${:.4f} {}",
            worker["id"],
            worker["status"],
            worker["seconds"],
            worker["events"],
            worker["usage_usd"],
            {k: round(v, 5) for k, v in worker["usage_by_item"].items()},
        )
    logger.log(
        "COST",
        "total ${:.4f} (events ${:.4f} + usage ${:.4f}) for {} page(s), {} unique organic "
        "results: ${:.4f}/page, ${:.5f}/result",
        costs["total_usd"],
        costs["events_usd"],
        costs["usage_usd"],
        pages,
        organic,
        costs["total_usd"] / max(pages, 1),
        costs["total_usd"] / max(organic, 1),
    )
    if costs["apify_fetch_events"] != costs["logged_events"].get("fetch", 0):
        logger.warning(
            "Apify counted {} fetch events, the log {}: did another client use the server?",
            costs["apify_fetch_events"],
            costs["logged_events"].get("fetch", 0),
        )


async def recheck(apify: ApifyClientAsync, run_dir: Path) -> int:
    """Read a finished run's costs again (Apify's usage figures settle late)."""
    state = json.loads((run_dir / "apify_state.json").read_text())
    calls = [
        Call(**json.loads(line)) for line in (run_dir / "calls.jsonl").read_text().splitlines()
    ]
    costs = await reconcile(apify, state, calls)
    (run_dir / "costs.json").write_text(json.dumps(costs, indent=2))
    pages = organic = 0
    for path in run_dir.glob("*.json"):  # noqa: ASYNC240 (a script, nothing else runs)
        doc = json.loads(path.read_text())
        if "query" in doc:
            pages += sum(1 for p in doc["pages"] if p.get("ok"))
            organic += doc["stats"]["unique_organic"]
    report(costs, pages, organic)
    return 0


# --- main -------------------------------------------------------------------


def setup_logging(out_dir: Path, verbose: bool, name: str = "run.log") -> None:
    logger.remove()
    logger.add(
        sys.stderr,
        level="DEBUG" if verbose else "INFO",
        colorize=True,
        format="<green>{time:HH:mm:ss.SSS}</green> <level>{level: <7}</level> {message}",
    )
    logger.add(
        out_dir / name,
        level="DEBUG",
        colorize=False,
        format="{time:YYYY-MM-DD HH:mm:ss.SSS} {level: <7} {message}",
    )


def token_from_env_file() -> str | None:
    """APIFY_TOKEN from a .env file in the repository root or the current
    directory (KEY=value lines), when it isn't in the environment."""
    for path in (HERE.parent / ".env", Path.cwd() / ".env"):
        if not path.is_file():
            continue
        for line in path.read_text().splitlines():
            key, sep, value = line.strip().partition("=")
            if sep and key.strip() == "APIFY_TOKEN" and value.strip():
                return value.strip().strip("'\"")
    return None


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("queries", nargs="*", help="search queries")
    parser.add_argument(
        "--pages", type=int, default=3, help="results pages per query (10 results each)"
    )
    parser.add_argument("--country", default="US", help="exit country and Google's gl= (ISO code)")
    parser.add_argument("--language", default="en", help="Google's hl=")
    parser.add_argument("--proxy", default="residential", choices=["residential", "datacenter"])
    parser.add_argument(
        "--format",
        default="html",
        choices=["html", "markdown"],
        help="html: everything (parsed by serp.py); markdown: the server's markdown, organic only",
    )
    parser.add_argument(
        "--resolve",
        type=int,
        default=0,
        metavar="N",
        help="follow the /goto links of the top N results for their URLs (a charged fetch each)",
    )
    parser.add_argument(
        "--max-mints", type=int, default=3, help="stop after this many mint_session calls"
    )
    parser.add_argument(
        "--session",
        help="a session handle from an earlier run (skips the first mint while it lives)",
    )
    parser.add_argument("--save-html", action="store_true", help="also keep each page's raw body")
    parser.add_argument("--out", type=Path, default=HERE / "runs")
    parser.add_argument("--verbose", action="store_true", help="also log mint progress")
    parser.add_argument("--recheck", type=Path, metavar="RUN_DIR", help="re-read a run's costs")
    args = parser.parse_args()

    token = os.environ.get("APIFY_TOKEN") or token_from_env_file()
    if not token:
        print("APIFY_TOKEN is not set: put it in .env (see .env.example)", file=sys.stderr)
        return 2
    apify = ApifyClientAsync(token)
    if args.recheck:
        setup_logging(args.recheck, args.verbose, "recheck.log")
        return await recheck(apify, args.recheck)
    if not args.queries:
        parser.error("give at least one query (or --recheck RUN_DIR)")
    out_dir = args.out / datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir.mkdir(parents=True, exist_ok=True)
    setup_logging(out_dir, args.verbose)
    ledger = Ledger(path=out_dir / "calls.jsonl")

    before = await standby_runs(apify)
    logger.info(
        "Apify: {} Standby run(s) of {} before we start{}",
        len(before),
        SERVER_ACTOR,
        "" if before else " (the first request starts one: expect a cold start)",
    )
    state: dict[str, Any] = {
        "before": {run_id: _snapshot(run) for run_id, run in before.items()},
        "server_runs": list(before),
        "jobs": ledger.jobs,
    }
    transport = StreamableHttpTransport(SERVER_URL, headers={"Authorization": f"Bearer {token}"})
    started = time.monotonic()
    docs = []
    async with Client(transport, timeout=300) as client:
        info = client.server_info
        logger.info(
            "connected to {} {} (MCP {}) in {:.1f}s",
            info.name if info else "?",
            info.version if info else "?",
            client.protocol_version,
            time.monotonic() - started,
        )
        tools = await client.list_tools()
        logger.info("tools: {}", ", ".join(t.name for t in tools))
        searcher = Searcher(
            Server(client, ledger), Exit(args.proxy, args.country, args.session), args.max_mints
        )
        for query in args.queries:
            docs.append(await run_query(searcher, query, args, out_dir))
        session = searcher.exit.session
    if session:
        (out_dir / "session.txt").write_text(session + "\n")
        logger.info(
            "session {} kept for an hour after its last use: --session $(cat {})",
            _short(session),
            out_dir / "session.txt",
        )
    failed = [c for c in ledger.calls if not c.ok]
    logger.info(
        "{} tool calls ({} failed) in {:.0f}s; output in {}",
        len(ledger.calls),
        len(failed),
        time.monotonic() - started,
        out_dir,
    )

    logger.info("reading the run's costs back from Apify …")
    await asyncio.sleep(10)  # let the last calls' usage reach the run records
    for run_id in await standby_runs(apify):
        if run_id not in state["server_runs"]:
            state["server_runs"].append(run_id)
    state["after"] = {}
    for run_id in state["server_runs"]:
        state["after"][run_id] = _snapshot(_run(await apify.run(run_id).get()))
    (out_dir / "apify_state.json").write_text(json.dumps(state, indent=2))
    try:
        costs = await reconcile(apify, state, ledger.calls)
    except Exception as error:  # the run still produced its data
        logger.error("could not read costs from Apify: {}", error)
    else:
        (out_dir / "costs.json").write_text(json.dumps(costs, indent=2))
        report(
            costs,
            sum(1 for d in docs for p in d["pages"] if p.get("ok")),
            sum(d["stats"]["unique_organic"] for d in docs),
        )
        logger.info(
            "proxy traffic is booked a few minutes late: for final figures run --recheck {}",
            out_dir,
        )
    return 0 if all(p.get("ok") for d in docs for p in d["pages"]) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
