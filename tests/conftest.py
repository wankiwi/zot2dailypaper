"""Root conftest: config fixtures and shared helpers.

All mocking uses pytest monkeypatch + SimpleNamespace. No unittest.mock.
"""

import copy

import pytest
from hydra import compose, initialize_config_dir
from hydra.core.global_hydra import GlobalHydra
from pathlib import Path

_CONFIG_DIR = str(Path(__file__).resolve().parent.parent / "config")


@pytest.fixture(autouse=True)
def isolated_price_checks(monkeypatch, tmp_path):
    """No live public checks or real price-state files in unit tests."""
    from zot2dailypaper import pricing
    pricing._attempts.clear()
    monkeypatch.setattr(pricing, 'CACHE_PATH', tmp_path / 'llm_pricing.json')

    def unavailable(*args):
        raise pricing.PricingUnavailable('offline_test')

    monkeypatch.setattr(pricing, '_fetch_public', unavailable)


@pytest.fixture(scope="session")
def _base_config():
    """Session-scoped Hydra config with all required values filled in.

    Never mutate this directly; use the function-scoped ``config`` fixture.
    """
    GlobalHydra.instance().clear()
    with initialize_config_dir(config_dir=_CONFIG_DIR, version_base=None):
        cfg = compose(
            config_name="legacy",
            overrides=[
                "zotero.user_id=000000",
                "zotero.api_key=fake-zotero-key",
                "zotero.include_path=null",
                "zotero.ignore_path=null",
                "email.sender=test@example.com",
                "email.receiver=test@example.com",
                "email.smtp_server=localhost",
                "email.smtp_port=1025",
                "email.sender_password=test",
                "llm.api.key=sk-fake",
                "llm.api.base_url=http://localhost:30000/v1",
                "llm.generation_kwargs.model=gpt-4o-mini",
                "reranker.api.key=sk-fake",
                "reranker.api.base_url=http://localhost:30000/v1",
                "reranker.api.model=text-embedding-3-large",
                "source.arxiv.category=[cs.AI,cs.CV]",
                "executor.source=[arxiv]",
                "executor.reranker=api",
                "executor.debug=false",
                "executor.send_empty=false",
            ],
        )
    return cfg


@pytest.fixture()
def config(_base_config, monkeypatch):
    """Function-scoped deep copy of the session config.

    Safe to mutate inside any test without polluting other tests.
    """
    # Pipeline unit tests stub providers; no remote budget writes may occur.
    from tests.canned_responses import make_budget_guard
    monkeypatch.setattr('zot2dailypaper.executor.prepare_budget', lambda _: make_budget_guard())
    monkeypatch.setattr('zot2dailypaper.executor.budget_plan', lambda _: (None, None))
    # Optional free metadata recovery has dedicated transport/integration tests.
    monkeypatch.setattr('zot2dailypaper.executor.recover_metadata', lambda *a, **kw: None)
    return copy.deepcopy(_base_config)
