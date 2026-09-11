"""Explicit, in-run web research with retained original bytes and provenance."""

from __future__ import annotations

import tempfile
import time
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from pathlib import Path

from synapse.gateway import Gateway
from synapse.revisions import durable_write
from synapse.source_extractors import prepare_source_file
from synapse.source_store import prepare_source
from synapse.util import generate_ulid
from synapse.v2_contracts import V2Error, canonical_json


class _HTMLText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.hidden = 0
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "noscript"}:
            self.hidden += 1
        if tag in {"p", "div", "br", "li", "h1", "h2", "h3", "tr"}:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in {"script", "style", "noscript"}:
            self.hidden = max(0, self.hidden - 1)
        if tag in {"p", "div", "li", "tr"}:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def fetch_page(url: str, *, timeout=30) -> tuple[bytes, str, str]:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in {"https", "http"} or not parsed.hostname or parsed.username or parsed.password:
        raise V2Error("invalid-request", "Research sources require an ordinary HTTP(S) page URL")
    request = urllib.request.Request(url, headers={"User-Agent": "Digital-Synapse/2.0 (owner-requested research)"})
    with urllib.request.urlopen(request, timeout=min(timeout, 30)) as response:
        raw = response.read(20_000_001)
        if len(raw) > 20_000_000:
            raise V2Error("coverage-limited", "Research source exceeds the 20 MB capture bound")
        return raw, response.headers.get_content_type(), response.geturl()


def prepare_web(raw: bytes, media_type: str, url: str, *, source_id=None, source_family_id=None):
    if media_type == "application/pdf":
        with tempfile.TemporaryDirectory(prefix="synapse-web-pdf-") as directory:
            path = Path(directory) / "source.pdf"
            path.write_bytes(raw)
            return prepare_source_file(path, origin=url, source_id=source_id, source_family_id=source_family_id)
    if media_type in {"text/html", "application/xhtml+xml"}:
        try:
            html = raw.decode("utf-8")
        except UnicodeDecodeError:
            return prepare_source(raw, origin=url, media_type="application/octet-stream", source_id=source_id, source_family_id=source_family_id, extraction={"method": "html-unsupported-encoding", "version": "1", "completeness": "failed"})
        parser = _HTMLText()
        parser.feed(html)
        text = "\n".join(line.strip() for line in "".join(parser.parts).splitlines() if line.strip())
        return prepare_source(raw, origin=url, media_type=media_type, source_id=source_id, source_family_id=source_family_id, text=text, extraction={"method": "html-visible-text", "version": "1", "completeness": "partial"})
    return prepare_source(raw, origin=url, media_type=media_type if media_type.startswith("text/") else "application/octet-stream", source_id=source_id, source_family_id=source_family_id)


class WebResearch:
    """A trusted host injects this callback into a requested specialist run."""

    def __init__(self, host, *, fetcher=fetch_page):
        self.host = host
        self.fetcher = fetcher

    def __call__(self, arguments, *, request, run_id, capability, timeout, cancelled, source_limit=3):
        if set(arguments) != {"query"} or not isinstance(arguments["query"], str) or not arguments["query"].strip():
            raise V2Error("invalid-request", "Research requires one meaningful query")
        run = self.host.publisher._run(capability, run_id, "investigate")
        instruction = request["owner_instruction_ref"]
        self.host._event(instruction)
        store = self.host.publisher.store
        pinned = store.manifest(run["knowledge_revision"])
        current = store.manifest()
        if pinned["records"] != current["records"] or pinned["sources"] != current["sources"]:
            raise V2Error("stale-selection", "Knowledge changed during research; explicitly continue against the new revision")
        deadline = time.monotonic() + timeout
        expected_sources = dict(pinned["sources"])
        urls = self.host.reasoner.search(arguments["query"], timeout=timeout, cancelled=cancelled)
        pages, failures = [], []
        for url in list(dict.fromkeys(urls))[:min(3, max(0, source_limit))]:
            if cancelled():
                raise V2Error("cancelled", "Research cancelled; prior captures remain retained")
            try:
                current = store.manifest()
                if current["records"] != pinned["records"] or current["sources"] != expected_sources:
                    raise V2Error("stale-selection", "Concurrent knowledge changed during research; explicitly continue to revalidate")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise V2Error("coverage-limited", "Research request deadline reached")
                raw, media_type, final_url = self.fetcher(url, timeout=remaining)
                final_url = urllib.parse.urldefrag(final_url)[0]
                if urllib.parse.urlsplit(final_url).scheme not in {"https", "http"}:
                    raise V2Error("invalid-request", "Research redirect has an unsupported scheme")
                previous = next((descriptor for descriptor in store.manifest()["source_versions"].values() if descriptor["origin"] == final_url), None)
                descriptor, objects = prepare_web(raw, media_type, final_url, source_id=previous["id"] if previous else None, source_family_id=previous["source_family_id"] if previous else None)
                receipt = self.host._capture(instruction, descriptor, objects, operation_id=generate_ulid(), request_id=request["id"])
                # Host capture may preserve its exact prepared descriptor on a
                # retry; resolve the committed source version from that receipt.
                captured = store.manifest(receipt["knowledge_revision"])
                descriptor = captured["source_versions"][captured["sources"][descriptor["id"]]]
                expected_sources[descriptor["id"]] = descriptor["version"]
                pages.append({"source": descriptor, "capture_receipt": receipt, "page": Gateway(self.host.vault, revision=receipt["knowledge_revision"]).source(descriptor["id"]) if descriptor.get("text_version") else None})
            except V2Error:
                raise
            except (OSError, ValueError) as exc:
                failures.append({"url": url, "reason": str(exc)})
        with store.writer_lock():
            active = self.host.publisher._run(capability, run_id, "investigate")
            current_revision = store.head()
            current = store.manifest(current_revision)
            if current["records"] != pinned["records"] or current["sources"] != expected_sources:
                raise V2Error("stale-selection", "Research sources were retained but record changes require explicit revalidation")
            active["knowledge_revision"] = current_revision
            active["trace"].append({"method": "capture-research", "query": arguments["query"], "source_ids": [item["source"]["id"] for item in pages], "previous_revision": run["knowledge_revision"], "knowledge_revision": current_revision, "records_unchanged": True})
            durable_write(store.root / "runs" / f"{run_id}.json", canonical_json(active))
        return {"knowledge_revision": current_revision, "pages": pages, "failures": failures, "limitations": ["Web captures establish what these pages said when captured, not independent owner facts or permanent truth."]}
