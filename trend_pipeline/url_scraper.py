"""Fetch readable text from one public webpage URL supplied by the user."""
from __future__ import annotations

import ipaddress
import logging
import re
import socket
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import Dict, List, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse
from urllib.request import (
    HTTPRedirectHandler,
    ProxyHandler,
    Request,
    build_opener,
)

from .ingest import parse_timestamp

logger = logging.getLogger("url_scraper")

MAX_HTML_BYTES = 2 * 1024 * 1024
MAX_BODY_CHARS = 20000
REQUEST_TIMEOUT_SECONDS = 20
MAX_REDIRECTS = 5
USER_AGENT = "R2C-ResearchContentBot/1.0 (+public-page extraction)"
SKIPPED_TAGS = {
    "aside",
    "button",
    "canvas",
    "footer",
    "form",
    "iframe",
    "nav",
    "noscript",
    "script",
    "style",
    "svg",
    "template",
}


def validate_public_url(url: str) -> str:
    """Accept only public HTTP(S) URLs and reject local/private destinations."""
    candidate = url.strip()
    parsed = urlparse(candidate)
    if parsed.scheme.lower() not in {"http", "https"}:
        raise ValueError("URL must start with http:// or https://.")
    if not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Enter a public webpage URL without embedded credentials.")
    if parsed.port not in (None, 80, 443):
        raise ValueError("Only standard HTTP/HTTPS ports are supported.")

    hostname = parsed.hostname.rstrip(".")
    if hostname.lower() == "localhost":
        raise ValueError("Localhost URLs cannot be scraped.")
    try:
        addresses = {
            item[4][0]
            for item in socket.getaddrinfo(
                hostname,
                parsed.port or (443 if parsed.scheme.lower() == "https" else 80),
            )
        }
    except socket.gaierror as exc:
        raise ValueError("Could not resolve the webpage hostname: {}".format(hostname)) from exc
    if not addresses:
        raise ValueError("The webpage hostname did not resolve to an IP address.")
    for address in addresses:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
        if not ip.is_global:
            raise ValueError("Private or non-public webpage addresses are not allowed.")
    return candidate


class _PublicRedirectHandler(HTTPRedirectHandler):
    """Validate each redirect destination before following it."""

    max_redirections = MAX_REDIRECTS
    max_repeats = MAX_REDIRECTS

    def redirect_request(self, request, response, code, message, headers, new_url):
        safe_url = validate_public_url(urljoin(request.full_url, new_url))
        return super().redirect_request(
            request, response, code, message, headers, safe_url
        )


class _VisibleTextParser(HTMLParser):
    """Collect page metadata and readable text while ignoring executable/UI tags."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.metadata: Dict[str, str] = {}
        self.title_parts: List[str] = []
        self.text_parts: List[str] = []
        self._skipped_depth = 0
        self._in_title = False

    def handle_starttag(self, tag: str, attrs) -> None:
        values = {key.lower(): value or "" for key, value in attrs if key is not None}
        if tag == "meta":
            metadata_key = (
                values.get("property")
                or values.get("name")
                or values.get("itemprop")
            )
            key = metadata_key.lower() if metadata_key else ""
            content = values.get("content", "").strip()
            if key and content:
                self.metadata.setdefault(key, content)
            return
        if tag in SKIPPED_TAGS:
            self._skipped_depth += 1
            return
        if self._skipped_depth == 0:
            if tag == "title":
                self._in_title = True
            if tag in {"article", "blockquote", "br", "div", "h1", "h2", "h3", "li", "main", "p"}:
                self.text_parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in SKIPPED_TAGS and self._skipped_depth:
            self._skipped_depth -= 1
            return
        if self._skipped_depth == 0:
            if tag == "title":
                self._in_title = False
            if tag in {"article", "blockquote", "br", "div", "h1", "h2", "h3", "li", "main", "p"}:
                self.text_parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skipped_depth:
            return
        text = data.strip()
        if not text:
            return
        if self._in_title:
            self.title_parts.append(text)
        self.text_parts.append(text)


def _header_value(headers, name: str) -> Optional[str]:
    getter = getattr(headers, "get", None)
    return getter(name) if callable(getter) else None


def _fetch_html(url: str) -> tuple[bytes, str, str]:
    """Fetch bounded HTML bytes, allowing only validated public redirects."""
    safe_url = validate_public_url(url)
    opener = build_opener(ProxyHandler({}), _PublicRedirectHandler())
    request = Request(
        safe_url,
        headers={
            "Accept": "text/html,application/xhtml+xml",
            "User-Agent": USER_AGENT,
        },
    )
    try:
        with opener.open(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            final_url = validate_public_url(response.geturl())
            content_type = _header_value(response.headers, "Content-Type") or ""
            media_type = content_type.split(";", 1)[0].strip().lower()
            if media_type not in {"text/html", "application/xhtml+xml"}:
                raise ValueError(
                    "URL returned {}, not an HTML webpage.".format(
                        media_type or "an unknown content type"
                    )
                )
            content_length = _header_value(response.headers, "Content-Length")
            if content_length and int(content_length) > MAX_HTML_BYTES:
                raise ValueError("Webpage is larger than the 2 MiB extraction limit.")
            body = response.read(MAX_HTML_BYTES + 1)
            if len(body) > MAX_HTML_BYTES:
                raise ValueError("Webpage is larger than the 2 MiB extraction limit.")
            charset_match = re.search(r"charset=([A-Za-z0-9._-]+)", content_type, re.I)
            charset = charset_match.group(1) if charset_match else "utf-8"
            return body, charset, final_url
    except HTTPError as exc:
        raise RuntimeError(
            "Webpage returned HTTP {}.".format(exc.code)
        ) from exc
    except URLError as exc:
        raise RuntimeError("Could not fetch webpage: {}".format(exc.reason)) from exc
    except (TimeoutError, socket.timeout) as exc:
        raise RuntimeError("Timed out while fetching webpage.") from exc


def scrape_url(url: str) -> Dict[str, List[Dict[str, object]]]:
    """Return the scraper's standard platform-to-records JSON shape for a URL."""
    body, charset, final_url = _fetch_html(url)
    try:
        html = body.decode(charset, errors="replace")
    except LookupError:
        html = body.decode("utf-8", errors="replace")

    parser = _VisibleTextParser()
    parser.feed(html)
    parser.close()
    metadata = parser.metadata
    domain = urlparse(final_url).hostname or ""
    title = (
        metadata.get("og:title")
        or metadata.get("twitter:title")
        or " ".join(parser.title_parts)
        or domain
    )
    description = (
        metadata.get("og:description")
        or metadata.get("description")
        or metadata.get("twitter:description")
        or ""
    )
    body_text = " ".join(" ".join(parser.text_parts).split())
    body_text = body_text[:MAX_BODY_CHARS]
    if description and description not in body_text:
        body_text = "{} {}".format(description, body_text).strip()
    if len((title + " " + body_text).strip()) < 40:
        raise ValueError(
            "The page did not expose enough readable text. It may require JavaScript, "
            "a login, or access not available to this public-page scraper."
        )

    published_at = (
        metadata.get("article:published_time")
        or metadata.get("datepublished")
        or metadata.get("date")
        or metadata.get("pubdate")
    )
    timestamp = parse_timestamp(published_at)
    retrieved_at = datetime.now(timezone.utc)
    return {
        "Webpage": [
            {
                "url": final_url,
                "title": title[:500],
                "body": body_text,
                "author": metadata.get("author") or domain,
                "created_at": (timestamp or retrieved_at).isoformat(),
                "retrieved_at": retrieved_at.isoformat(),
                "domain": domain,
            }
        ]
    }
