"""Tests for ArxivRetriever."""

import time
from types import SimpleNamespace
from contextlib import nullcontext

import pytest


from zot2dailypaper.retriever.arxiv_retriever import ArxivRetriever, _run_with_hard_timeout
import zot2dailypaper.retriever.arxiv_retriever as arxiv_retriever


def _sleep_and_return(value: str, delay_seconds: float) -> str:
    time.sleep(delay_seconds)
    return value


def _raise_runtime_error() -> None:
    raise RuntimeError("boom")


def test_arxiv_retriever(config, mock_arxiv_feed, monkeypatch):
    monkeypatch.setattr("zot2dailypaper.retriever.base.sleep", lambda _: None)

    # The RSS fixture gives us paper IDs.  After feedparser, the code calls
    # arxiv.Client().results(search) which makes real HTTP requests.  We mock
    # the arxiv Client so the test stays offline.
    new_entries = [
        e for e in mock_arxiv_feed.entries
        if e.get("arxiv_announce_type", "new") == "new"
    ]

    # Build fake ArxivResult-like objects matching each RSS entry
    fake_results = []
    for entry in new_entries:
        pid = entry.id.removeprefix("oai:arXiv.org:")
        fake_results.append(SimpleNamespace(
            title=entry.title,
            authors=[SimpleNamespace(name="Test Author")],
            summary="Test abstract",
            pdf_url=f"https://arxiv.org/pdf/{pid}",
            entry_id=f"https://arxiv.org/abs/{pid}",
            source_url=lambda pid=pid: f"https://arxiv.org/e-print/{pid}",
        ))

    class FakeClient:
        def __init__(self, **kw):
            pass
        def results(self, search):
            return iter(fake_results)

    monkeypatch.setattr(arxiv_retriever.arxiv, "Client", FakeClient)

    # Skip file downloads in convert_to_paper
    monkeypatch.setattr(arxiv_retriever, "extract_text_from_html", lambda paper: None)
    monkeypatch.setattr(arxiv_retriever, "extract_text_from_pdf", lambda paper: None)
    monkeypatch.setattr(arxiv_retriever, "extract_text_from_tar", lambda paper: None)

    retriever = ArxivRetriever(config)
    papers = retriever.retrieve_papers()

    assert len(papers) == len(new_entries)
    assert set(p.title for p in papers) == set(e.title for e in new_entries)


@pytest.mark.parametrize("status,content,error", [
    (403, b"", "HTTP 403"),
    (429, b"", "HTTP 429"),
    (503, b"", "HTTP 503"),
    (200, b"<html><body>Temporary error</body></html>", "not a complete RSS/Atom feed"),
    (200, b'<feed xmlns="http://www.w3.org/2005/Atom"><entry>', "not a complete RSS/Atom feed"),
])
def test_failed_announcement_feed_is_not_recorded_as_empty_success(config, monkeypatch, status, content, error):
    def get(url, **kwargs):
        assert url.startswith("https://rss.arxiv.org/atom/")
        assert kwargs == {"timeout": (5, 20), "allow_redirects": False}
        return nullcontext(SimpleNamespace(status_code=status, content=content))

    monkeypatch.setattr(arxiv_retriever.requests, "get", get)
    monkeypatch.setattr(arxiv_retriever, "record_health", lambda *args: pytest.fail("Must not record failed feed"))
    with pytest.raises(arxiv_retriever.ArxivFeedError, match=error):
        ArxivRetriever(config).retrieve_papers()


def test_valid_empty_announcement_feed_is_successful(config, monkeypatch):
    content = b'<feed xmlns="http://www.w3.org/2005/Atom"><title>Latest papers</title></feed>'
    monkeypatch.setattr(arxiv_retriever.requests, "get", lambda *args, **kwargs:
                        nullcontext(SimpleNamespace(status_code=200, content=content)))
    health = []
    monkeypatch.setattr(arxiv_retriever, "record_health", lambda *args: health.append(args[2]))
    assert ArxivRetriever(config).retrieve_papers() == []
    assert len(health) == 1 and health[0]["api_entries"] == 0


def test_run_with_hard_timeout_returns_value():
    result = _run_with_hard_timeout(
        _sleep_and_return, ("done", 0.01), timeout=1, operation="test op", paper_title="paper"
    )
    assert result == "done"


def test_run_with_hard_timeout_returns_none_on_timeout(monkeypatch):
    warnings: list[str] = []
    monkeypatch.setattr(arxiv_retriever, "logger", SimpleNamespace(warning=warnings.append))
    result = _run_with_hard_timeout(
        _sleep_and_return, ("done", 1.0), timeout=0.01, operation="test op", paper_title="paper"
    )
    assert result is None
    assert "timed out" in warnings[0]


def test_run_with_hard_timeout_returns_none_on_failure(monkeypatch):
    warnings: list[str] = []
    monkeypatch.setattr(arxiv_retriever, "logger", SimpleNamespace(warning=warnings.append))
    result = _run_with_hard_timeout(
        _raise_runtime_error, (), timeout=1, operation="test op", paper_title="paper"
    )
    assert result is None
    assert "boom" in warnings[0]


def test_conversion_defers_full_text_until_selected(config, monkeypatch):
    raw = SimpleNamespace(title='Selected paper', authors=[], summary='Abstract', pdf_url='https://arxiv.org/pdf/2401.01234', entry_id='https://arxiv.org/abs/2401.01234')
    calls = []
    monkeypatch.setattr(arxiv_retriever, 'extract_text_from_html', lambda paper: calls.append('html') or 'Full text')
    monkeypatch.setattr(arxiv_retriever, 'extract_text_from_pdf', lambda paper: calls.append('pdf'))
    retriever = ArxivRetriever(config)
    paper = retriever.convert_to_paper(raw)
    assert paper.full_text is None and calls == []
    assert retriever.enrich(paper).full_text == 'Full text'
    assert calls == ['html']
