"""Web search through your own SearXNG instance (SEARXNG_URL, e.g. http://your-server:8080; SEARXNG_KEY is sent as
X-API-Key when set, for an instance behind a proxy that checks it). Used to find decision makers: each result's title
and snippet are read like a search results page. The instance must allow the JSON format (search.formats in its
settings.yml)."""
import json
import os
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

TIMEOUT = 20
USER_AGENT = "Mozilla/5.0 (compatible; outreach-app)"


class SearchUnavailable(RuntimeError):
    """SearXNG cannot be reached or answered something unusable."""


def base_url() -> str:
    return os.environ.get("SEARXNG_URL", "").strip().rstrip("/")


def configured() -> bool:
    return bool(base_url())


def search(query: str, language: str = "en", timeout: float = TIMEOUT) -> list[dict]:
    """[{url, title, content, engine}] for one query, best first."""
    if not configured():
        raise SearchUnavailable("SEARXNG_URL is not set")
    headers = {"Accept": "application/json", "User-Agent": USER_AGENT}
    key = os.environ.get("SEARXNG_KEY", "").strip()
    if key:
        headers["X-API-Key"] = key
    url = f"{base_url()}/search?" + urlencode({"q": query, "format": "json", "language": language, "safesearch": 0})
    try:
        with urlopen(Request(url, headers=headers), timeout=timeout) as response:  # noqa: S310 - our own setting
            data = json.loads(response.read().decode("utf-8", "replace"))
    except HTTPError as error:
        hint = " (is the JSON format enabled in its settings.yml?)" if error.code == 403 else ""
        raise SearchUnavailable(f"SearXNG answered HTTP {error.code}{hint}") from error
    except (URLError, OSError) as error:
        raise SearchUnavailable(f"SearXNG at {base_url()} is not reachable ({getattr(error, 'reason', error)})") from error
    except ValueError as error:
        raise SearchUnavailable("SearXNG did not answer JSON") from error
    results = data.get("results") if isinstance(data, dict) else None
    if not isinstance(results, list):
        raise SearchUnavailable("SearXNG answered without results")
    return [{"url": str(item.get("url") or ""), "title": str(item.get("title") or ""),
             "content": str(item.get("content") or ""), "engine": str(item.get("engine") or "")}
            for item in results if isinstance(item, dict) and item.get("url")]
