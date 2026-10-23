"""Synthetic catalog IDs in fixtures are not verified real-world mappings."""
from copy import deepcopy
from datetime import datetime, timezone
from types import SimpleNamespace
import pytest
from omegaconf import open_dict
from zot2dailypaper.retriever.researchsquare_retriever import ResearchSquareRetriever
from zot2dailypaper.retriever.openalex_researchsquare import abstract_text
from tests.test_preprint_interests import _profile

SOURCES = ['S4306525896', 'S4306402450']

def item(number=1, **changes):
    return dict(id=f'https://openalex.org/W{number}', doi=f'https://doi.org/10.21203/rs.3.rs-{number}/v1',
        title=f'Study {number}', type='preprint', publication_date=datetime.now(timezone.utc).date().isoformat(),
        locations=[{'source': {'id': f'https://openalex.org/{s}'}} for s in SOURCES],
        topics=[{'subfield': {'id': 'https://openalex.org/subfields/1000'}}],
        primary_topic={'subfield': {'id': 'https://openalex.org/subfields/9999'}},
        authorships=[{'author': {'display_name': 'A Scientist'}}],
        abstract_inverted_index={'study': [1], 'A': [0]}, is_retracted=False) | changes


def setup(config, monkeypatch, pages, source_name='Research Square', catalog=None, status=200):
    config.preprint_interests = deepcopy(_profile().preprint_interests)
    # Multi-source regression fixtures are independent of the active single-source profile.
    config.preprint_interests.researchsquare.source_ids = SOURCES
    names = list(config.preprint_interests.researchsquare.subfield)
    if catalog is None:
        catalog = [{'id': f'https://openalex.org/subfields/{1000+i}', 'display_name': n} for i, n in enumerate(names)]
    calls = []
    class Client:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def get(self, url, params=None, **kwargs):
            calls.append((url, params))
            if '/sources/' in url:
                data = {'id': url.replace('api.openalex.org/sources', 'openalex.org'), 'display_name': source_name}
            elif url.endswith('/subfields'):
                start = (params['page'] - 1) * 100
                data = {'meta': {'count': len(catalog)}, 'results': catalog[start:start + 100]}
            else:
                data = pages.pop(0)
            return SimpleNamespace(status_code=status, raise_for_status=lambda: None, json=lambda: data, close=lambda: None)
    monkeypatch.setattr('zot2dailypaper.retriever.openalex_researchsquare.session', Client)
    return ResearchSquareRetriever(config), calls


def page(items, count=None, cursor=None):
    return {'meta': {'count': len(items) if count is None else count, 'next_cursor': cursor}, 'results': items}


def test_server_union_subfields_cursor_and_duplicates(config, monkeypatch):
    retriever, calls = setup(config, monkeypatch, [page([item()], 3, 'next'), page([item(), item(2)], 3)])
    papers = retriever.retrieve_papers()
    assert len(papers) == 2
    works = [params for url, params in calls if url.endswith('/works')]
    assert works[0]['filter'].startswith('locations.source.id:S4306525896|S4306402450,type:preprint,topics.subfield.id:')
    assert 'from_publication_date:' in works[0]['filter'] and 'to_publication_date:' in works[0]['filter']
    assert 'primary_topic' not in works[0]['filter']
    assert works[0]['per_page'] == 100 and works[1]['cursor'] == 'next'
    assert papers[0].abstract == 'A study' and papers[0].authors == ['A Scientist']


def test_missing_abstract_doi_and_legacy_keywords(config, monkeypatch):
    setup(config, monkeypatch, [page([item(doi=None, abstract_inverted_index=None)])])
    with open_dict(config.preprint_interests.researchsquare):
        config.preprint_interests.researchsquare.keywords = ['unrelated impossible phrase']
    papers = ResearchSquareRetriever(config).retrieve_papers()
    assert len(papers) == 1 and papers[0].abstract == '' and papers[0].doi is None
    assert papers[0].url == 'https://openalex.org/W1'


@pytest.mark.parametrize('changes', [{'publication_date': '2000-01-01'}, {'publication_date': 'invalid'},
    {'type': 'article'}, {'locations': []}, {'topics': []}, {'doi': {}}, {'topics': None}])
def test_scope_recheck_and_malformed_isolation(config, monkeypatch, changes):
    retriever, _ = setup(config, monkeypatch, [page([item(2, **changes), item()])])
    assert len(retriever.retrieve_papers()) == 1


@pytest.mark.parametrize('change', [{'is_retracted': True}, {'title': 'WITHDRAWN: study'}])
def test_latest_withdrawn_suppresses_older(config, monkeypatch, change):
    retriever, _ = setup(config, monkeypatch, [page([item(), item(2, doi='https://doi.org/10.21203/rs.3.rs-1/v2', **change)])])
    assert retriever.retrieve_papers() == []


@pytest.mark.parametrize('pages', [[page([item()], 2, None)], [page([], 1)], [page([item()], 3, '*')],
    [page([item()], 3, 'next'), page([item(2)], 3, 'next')]])
def test_incomplete_pagination(config, monkeypatch, pages):
    retriever, _ = setup(config, monkeypatch, pages)
    with pytest.raises(RuntimeError, match='pagination'): retriever.retrieve_papers()


def test_page_limit(config, monkeypatch):
    config.source.researchsquare.max_pages = 1
    retriever, _ = setup(config, monkeypatch, [page([item()], 2, 'next')])
    with pytest.raises(RuntimeError, match='max_pages'): retriever.retrieve_papers()


def test_wrong_source_identity(config, monkeypatch):
    retriever, calls = setup(config, monkeypatch, [], source_name='Unrelated repository')
    with pytest.raises(ValueError, match='verified Research Square'): retriever.retrieve_papers()
    assert not any(url.endswith('/works') for url, _ in calls)


def test_missing_subfield(config, monkeypatch):
    retriever, calls = setup(config, monkeypatch, [], catalog=[])
    with pytest.raises(ValueError, match='missing from complete catalog'): retriever.retrieve_papers()
    assert not any(url.endswith('/works') for url, _ in calls)


@pytest.mark.parametrize('status', [401, 403, 429])
def test_access_budget_no_fallback(config, monkeypatch, status):
    retriever, _ = setup(config, monkeypatch, [], status=status)
    with pytest.raises(RuntimeError, match='no paid fallback'): retriever.retrieve_papers()


@pytest.mark.parametrize('field,value', [('source_ids', []), ('source_ids', ['S123|S456']), ('type', ['article']), ('subfield', []), ('backend', 'unknown')])
def test_invalid_configuration(config, field, value):
    config.preprint_interests = deepcopy(_profile().preprint_interests)
    config.preprint_interests.researchsquare[field] = value
    with pytest.raises(ValueError): ResearchSquareRetriever(config)


def test_untrusted_abstract_offsets():
    assert abstract_text({'ok': [0], 'large': [10**30], 'bad': [-1, 'x', True], 'invalid': None}) == 'ok'


def test_subfield_catalog_pagination_and_exact_names(config, monkeypatch):
    names = list(_profile().preprint_interests.researchsquare.subfield)
    catalog = [{'id': f'https://openalex.org/subfields/{2000+i}', 'display_name': f'Other {i}'} for i in range(100)]
    catalog += [{'id': f'https://openalex.org/subfields/{1000+i}', 'display_name': n} for i, n in enumerate(names)]
    retriever, calls = setup(config, monkeypatch, [page([item()])], catalog=catalog)
    assert len(retriever.retrieve_papers()) == 1
    assert [params['page'] for url, params in calls if url.endswith('/subfields')] == [1, 2]
    assert len([url for url, _ in calls if '/sources/' in url]) == 2


def test_same_name_subfields_are_unioned_without_guessing(config, monkeypatch):
    names = list(_profile().preprint_interests.researchsquare.subfield)
    catalog = [{'id': f'https://openalex.org/subfields/{1000+i}', 'display_name': n} for i, n in enumerate(names)]
    catalog.append({'id': 'https://openalex.org/subfields/8888', 'display_name': names[0]})
    retriever, calls = setup(config, monkeypatch, [page([])], catalog=catalog)
    assert retriever.retrieve_papers() == []
    filters = next(params['filter'] for url, params in calls if url.endswith('/works'))
    assert '1000' in filters and '8888' in filters


def test_zero_results_is_success(config, monkeypatch):
    retriever, _ = setup(config, monkeypatch, [page([])])
    assert retriever.retrieve_papers() == []


def test_one_missing_source_preserves_other_and_reports_failure(config, monkeypatch):
    from zot2dailypaper.retriever.openalex_researchsquare import MissingOpenAlexRecord
    import zot2dailypaper.retriever.openalex_researchsquare as module
    retriever,calls=setup(config, monkeypatch, [page([item()])])
    original=module.get_json
    def get(client,path,params=None):
        if path.endswith(SOURCES[1]):raise MissingOpenAlexRecord('404')
        return original(client,path,params)
    monkeypatch.setattr(module,'get_json',get)
    assert len(retriever.retrieve_papers())==1
    assert retriever.failures == [f'{SOURCES[1]}: OpenAlex source HTTP 404']
    params=[p for url,p in calls if url.endswith('/works')][0]
    assert f'locations.source.id:{SOURCES[0]},' in params['filter']
    assert SOURCES[1] not in params['filter']


def test_all_missing_sources_do_not_query_works(config, monkeypatch):
    retriever,calls=setup(config, monkeypatch, [], status=404)
    with pytest.raises(ValueError, match='No verified'):
        retriever.retrieve_papers()
    assert len(retriever.failures)==2
    assert not any(url.endswith('/works') for url,_ in calls)


@pytest.mark.parametrize('index,expected',[
    ({'T&lt;Tc':[0],'and':[1],'x&gt;0':[2]},'T<Tc and x>0'),
    ({'T<Tc':[0],'and':[1],'x>0':[2]},'T<Tc and x>0'),
    ({'<p>Molecular</p>':[0],'dynamics':[1]},'Molecular dynamics'),
    ({'No':[0],'abstract':[1],'available':[2]},''),
    ({'valid':[1],'bad':[True,-1,100000],'next':[3]},'valid next'),
])
def test_shared_abstract_decoder_preserves_science_and_placeholders(index,expected):
    from zot2dailypaper.abstracts import inverted_abstract
    from zot2dailypaper.retriever.openalex_researchsquare import abstract_text
    assert abstract_text(index)==inverted_abstract(index)==expected
