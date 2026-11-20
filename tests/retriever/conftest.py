"""Retriever-specific fixtures."""

import feedparser
import pytest


@pytest.fixture()
def mock_arxiv_feed(monkeypatch):
    """Serve the fixture without turning feedparser's byte parsing into a mock."""
    from pathlib import Path
    from types import SimpleNamespace

    import requests

    raw = (Path(__file__).parent / "arxiv_rss_example.xml").read_bytes()
    original_get = requests.get

    def get(url, **kwargs):
        if url.startswith("https://rss.arxiv.org/atom/"):
            from contextlib import nullcontext
            return nullcontext(SimpleNamespace(status_code=200, content=raw))
        return original_get(url, **kwargs)

    monkeypatch.setattr(requests, "get", get)
    return feedparser.parse(raw)


@pytest.fixture()
def mock_biorxiv_api(monkeypatch):
    """Patch requests.get to return the canned bioRxiv API response."""
    import requests
    from types import SimpleNamespace

    from tests.canned_responses import SAMPLE_BIORXIV_API_RESPONSE

    original_get = requests.get

    def _patched(url, **kwargs):
        if "api.biorxiv.org" in url:
            resp = SimpleNamespace()
            resp.status_code = 200
            resp.json = lambda: SAMPLE_BIORXIV_API_RESPONSE
            resp.raise_for_status = lambda: None
            return resp
        return original_get(url, **kwargs)

    monkeypatch.setattr(requests, "get", _patched)
