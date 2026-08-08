"""A single fixed-host web search tool with no arbitrary URL fetching."""

from __future__ import annotations

from html.parser import HTMLParser
import re
import urllib.error
import urllib.parse
import urllib.request


class SearchError(RuntimeError):
    pass


class _DuckDuckGoParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.results: list[dict[str, str]] = []
        self._capture: str | None = None
        self._buffer: list[str] = []
        self._pending_title = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        classes = dict(attrs).get("class", "") or ""
        if tag == "a" and "result__a" in classes:
            self._capture = "title"
            self._buffer = []
        elif tag in {"a", "div"} and "result__snippet" in classes:
            self._capture = "snippet"
            self._buffer = []

    def handle_data(self, data: str) -> None:
        if self._capture:
            self._buffer.append(data)

    def handle_endtag(self, tag: str) -> None:
        if self._capture == "title" and tag == "a":
            self._pending_title = _clean(" ".join(self._buffer))
            self._capture = None
        elif self._capture == "snippet" and tag in {"a", "div"}:
            snippet = _clean(" ".join(self._buffer))
            if self._pending_title and snippet:
                self.results.append({"title": self._pending_title, "snippet": snippet})
            self._pending_title = ""
            self._capture = None


def _clean(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()[:300]


class FixedHostWebSearch:
    endpoint = "https://html.duckduckgo.com/html/"

    def __init__(self, timeout_seconds: int = 6, max_results: int = 3):
        self.timeout_seconds = timeout_seconds
        self.max_results = max(1, min(max_results, 5))

    def search(self, query: str) -> list[dict[str, str]]:
        query = _clean(query)[:120]
        if not query:
            raise SearchError("empty_query")
        url = f"{self.endpoint}?{urllib.parse.urlencode({'q': query})}"
        request = urllib.request.Request(
            url,
            headers={"User-Agent": "Mozilla/5.0 (compatible; SleepyPetAI/1.0)"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                raw = response.read(524_289)
                if len(raw) > 524_288:
                    raise SearchError("response_too_large")
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise SearchError("search_unavailable") from exc
        parser = _DuckDuckGoParser()
        try:
            parser.feed(raw.decode("utf-8", errors="replace"))
        except Exception as exc:
            raise SearchError("invalid_search_response") from exc
        if not parser.results:
            raise SearchError("no_results")
        return parser.results[: self.max_results]
