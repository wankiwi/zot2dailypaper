"""Safety and reporting checks for the one-off diagnostic, entirely offline."""
import json

import pytest
import requests
import yaml

from scripts import diagnose_arxiv_live as script
from tests.test_arxiv_integrity import entry, page


@pytest.mark.parametrize('method,url,redirects,expected', [
    ('GET', 'https://export.arxiv.org/api/query?start=0', False, True),
    ('POST', 'https://export.arxiv.org/api/query', False, False),
    ('GET', 'https://export.arxiv.org/api/query', True, False),
    ('GET', 'https://arxiv.org/api/query', False, False),
    ('GET', 'https://api.openai.com/v1/models', False, False),
    ('GET', 'https://api.zotero.org/users/1/items', False, False),
])
def test_only_the_official_read_only_query_is_permitted(method, url, redirects, expected):
    assert script.allowed_request(method, url, redirects) is expected


def test_raw_metadata_is_reported_without_dumping_abstracts():
    response = requests.Response()
    response.status_code = 200
    response._content = page(entry(1), total=1)
    facts, samples = script.page_facts(response)
    assert facts['http'] == 200 and facts['entries'] == 1 and facts['totalResults'] == '1'
    assert samples[0]['id'].endswith('2610.00001v1')
    assert 'Complete original abstract' not in json.dumps((facts, samples))


def test_cloud_environment_cannot_execute_the_live_diagnostic(monkeypatch):
    monkeypatch.delenv('GITHUB_ACTIONS', raising=False)
    with pytest.raises(RuntimeError, match='dedicated read-only'):
        script.main()


def test_workflow_has_no_secrets_state_cache_or_delivery_steps():
    text = (script.ROOT / '.github/workflows/arxiv-live-diagnostic.yml').read_text()
    config = yaml.load(text, Loader=yaml.BaseLoader)
    assert config['permissions'] == {'contents': 'read'}
    assert config['on']['push']['branches'] == ['diagnostics/arxiv-live-20261020']
    assert 'secrets.' not in text and 'workflow_state.py' not in text
    assert 'zot2dailypaper.main' not in text and 'actions/cache' not in text
    assert 'enable-cache: false' in text and 'persist-credentials: false' in text
