from types import SimpleNamespace
import numpy as np
from omegaconf import open_dict
import pytest
from xml.etree import ElementTree as ET

from tests.canned_responses import make_sample_paper, make_stub_zotero_client
from zot2dailypaper.executor import Executor
from zot2dailypaper.state import State
from zot2dailypaper.reranker.api import ApiReranker
from zot2dailypaper.reranker.base import BaseReranker


@pytest.fixture()
def pipeline(config, tmp_path, monkeypatch):
    with open_dict(config):
        config.executor.source = ['arxiv']
        config.executor.fetch_full_text = False
        config.executor.reranker = 'api'
        config.llm.enabled = False
        config.output.email.enabled = False
        config.output.rss.enabled = True
        config.output.rss.path = str(tmp_path / 'feed.xml')
        config.state.path = str(tmp_path / 'state.json')
    monkeypatch.setattr('zot2dailypaper.executor.zotero.Zotero', lambda *a, **kw: make_stub_zotero_client())
    def similarity(self, left, right):
        return np.ones((len(left), len(right))) * .8
    monkeypatch.setattr(ApiReranker, 'get_similarity_score', similarity)
    return config


def test_rss_only_does_not_resolve_smtp_or_llm_credentials(pipeline, monkeypatch):
    with open_dict(pipeline):
        pipeline.email = {'sender': '???'}
        pipeline.llm.api = {'key': '???'}
    executor = Executor(pipeline)
    monkeypatch.setattr(executor.retrievers['arxiv'], 'retrieve_papers', lambda: [make_sample_paper()])
    executor.run()
    assert len(ET.parse(pipeline.output.rss.path).findall('./channel/item')) == 1
    repeat = Executor(pipeline)
    monkeypatch.setattr(repeat.retrievers['arxiv'], 'retrieve_papers', lambda: [make_sample_paper()])
    monkeypatch.setattr(repeat.reranker, 'rerank', lambda *a: pytest.fail('Already delivered papers must not be ranked again'))
    repeat.run()
    assert len(ET.parse(pipeline.output.rss.path).findall('./channel/item')) == 1


def test_email_failure_does_not_block_rss_and_retries_without_retrieval(pipeline, monkeypatch):
    with open_dict(pipeline):
        pipeline.output.email.enabled = True
    executor = Executor(pipeline)
    monkeypatch.setattr(executor.retrievers['arxiv'], 'retrieve_papers', lambda: [make_sample_paper()])
    monkeypatch.setattr('zot2dailypaper.executor.send_email', lambda *a: (_ for _ in ()).throw(OSError('smtp down')))
    with pytest.raises(RuntimeError, match='smtp down'):
        executor.run()
    state = State(pipeline.state.path)
    assert state.pending('email') and not state.pending('rss')
    assert len(ET.parse(pipeline.output.rss.path).findall('./channel/item')) == 1
    sent = []
    monkeypatch.setattr('zot2dailypaper.executor.send_email', lambda *a: sent.append(a))
    repeat = Executor(pipeline)
    monkeypatch.setattr(repeat.retrievers['arxiv'], 'retrieve_papers', lambda: [])
    repeat.run()
    assert len(sent) == 1 and not State(pipeline.state.path).pending('email')


def test_enrichment_runs_only_for_selected_papers(pipeline, monkeypatch):
    with open_dict(pipeline):
        pipeline.executor.max_paper_num = 1
        pipeline.executor.fetch_full_text = True
        pipeline.llm.input_mode = 'full_text'
        pipeline.llm.enabled = True
    from tests.canned_responses import make_stub_openai_client
    monkeypatch.setattr('zot2dailypaper.executor.OpenAI', lambda **kwargs: make_stub_openai_client())
    executor = Executor(pipeline)
    papers = [make_sample_paper(title=f'Paper {i}', url=f'https://example.org/{i}') for i in range(3)]
    monkeypatch.setattr(executor.retrievers['arxiv'], 'retrieve_papers', lambda: papers)
    enriched = []
    def enrich(p):
        enriched.append(p.title)
        return p
    monkeypatch.setattr(executor.retrievers['arxiv'], 'enrich', enrich)
    executor.run()
    assert len(enriched) == 1


def test_partial_source_failure_is_not_reported_as_success(pipeline, monkeypatch):
    executor = Executor(pipeline)
    monkeypatch.setattr(executor.retrievers['arxiv'], 'retrieve_papers', lambda: (_ for _ in ()).throw(OSError('feed down')))
    with pytest.raises(RuntimeError, match='feed down'):
        executor.run()


def test_invalid_arxiv_later_page_preserves_other_source_delivery_and_dedup(pipeline, monkeypatch):
    from tests.test_arxiv_integrity import entry, page, transport
    pipeline.source.arxiv.window_days = 7
    pipeline.source.arxiv.category = ['cs.AI']
    pipeline.output.email.enabled = True
    pipeline.executor.send_empty = False
    calls = transport(monkeypatch, [page(entry(1), total=2), b'<html>bad page</html>'] * 2)
    sent = []
    monkeypatch.setattr('zot2dailypaper.executor.send_email', lambda *args: sent.append(args))
    other = make_sample_paper(source='journals', title='Independent journal paper', doi='10.5555/independent-journal')
    for _ in range(2):
        executor = Executor(pipeline)
        executor.retrievers['journals'] = SimpleNamespace(retrieve_papers=lambda: [other])
        with pytest.raises(RuntimeError, match='arxiv: arXiv HTTP 200 is not an Atom feed'):
            executor.run()
    saved = State(pipeline.state.path)
    assert len(calls) == 4 and len(sent) == 1
    assert len(saved.records) == 1 and next(iter(saved.records.values()))['paper']['source'] == 'journals'
    assert not saved.pending('email') and not saved.pending('rss')


def test_abstract_recovery_budget_targets_selected_papers(pipeline, monkeypatch):
    with open_dict(pipeline):
        pipeline.executor.max_paper_num = 1
        pipeline.abstracts = {'enabled': True, 'max_papers': 1}
    executor = Executor(pipeline)
    papers = [make_sample_paper(title=f'Candidate {i}', abstract='',
              doi=f'10.1234/paper{i}', url=f'https://example.org/{i}') for i in range(60)]
    monkeypatch.setattr(executor.retrievers['arxiv'], 'retrieve_papers', lambda: papers)
    def rank(items, corpus):
        for i, p in enumerate(items):
            p.score, p.scoring_basis = i, 'abstract' if p.abstract else 'title only'
        return list(reversed(items))
    monkeypatch.setattr(executor.reranker, 'rerank', rank)
    looked_up = []
    def recover(items, config):
        assert config.max_papers == 1 and len(items) == 1
        looked_up.extend(items)
        items[0].abstract = 'Recovered original abstract'
    monkeypatch.setattr('zot2dailypaper.executor.recover_abstracts', recover)
    executor.run()
    assert looked_up == [papers[-1]]
    saved = next(iter(State(pipeline.state.path).records.values()))['paper']
    assert saved['abstract'] == 'Recovered original abstract'
    assert saved['scoring_basis'] == 'abstract'
    assert saved['selection_score'] == 59


def test_missing_zotero_collection_and_abstract_are_handled(pipeline, monkeypatch):
    items = [{'data': {'title': 'Title', 'dateAdded': '2026-03-02T00:00:00Z', 'abstractNote': 'Useful', 'collections': ['missing']}},
             {'data': {'title': 'No abstract', 'dateAdded': '2026-03-02T00:00:00Z'}}]
    monkeypatch.setattr('zot2dailypaper.executor.zotero.Zotero', lambda *a, **kw: make_stub_zotero_client(items=items))
    corpus = Executor(pipeline).fetch_zotero_corpus()
    assert len(corpus) == 1 and corpus[0].paths == []


def test_collection_cycles_raise_useful_error(pipeline, monkeypatch):
    collections = [{'key': 'COL1', 'data': {'name': 'A', 'parentCollection': 'COL2'}}, {'key': 'COL2', 'data': {'name': 'B', 'parentCollection': 'COL1'}}]
    monkeypatch.setattr('zot2dailypaper.executor.zotero.Zotero', lambda *a, **kw: make_stub_zotero_client(collections=collections))
    with pytest.raises(ValueError, match='Cycle'):
        Executor(pipeline).fetch_zotero_corpus()


def test_embeddings_batch_unique_texts_and_restore_response_indices(config, monkeypatch):
    calls = []
    def create(input, **kwargs):
        calls.append(input)
        return SimpleNamespace(data=[SimpleNamespace(index=i, embedding=[i + 1., 1.]) for i in reversed(range(len(input)))])
    monkeypatch.setattr('zot2dailypaper.reranker.api.OpenAI', lambda **kw: SimpleNamespace(embeddings=SimpleNamespace(create=create)))
    reranker = ApiReranker(config)
    first = reranker.get_similarity_score(['same', 'other', 'same'], ['same'])
    assert calls == [['same', 'other']]
    assert first[0, 0] == pytest.approx(1) and first[0, 0] == first[2, 0]
    reranker.get_similarity_score(['same'], ['other'])
    assert len(calls) == 1


def test_zero_embedding_is_rejected(config, monkeypatch):
    client = SimpleNamespace(embeddings=SimpleNamespace(create=lambda **kw: SimpleNamespace(data=[SimpleNamespace(index=0, embedding=[0., 0.])])) )
    monkeypatch.setattr('zot2dailypaper.reranker.api.OpenAI', lambda **kw: client)
    with pytest.raises(ValueError, match='zero vector'):
        ApiReranker(config).get_similarity_score(['same'], ['same'])


def test_missing_abstract_ranks_with_title(config):
    class Reranker(BaseReranker):
        def get_similarity_score(self, left, right):
            assert left == ['A title']
            return np.ones((1, 1))
    from tests.canned_responses import make_sample_corpus
    paper = make_sample_paper(title='A title', abstract='')
    assert Reranker(config).rerank([paper], make_sample_corpus(1))[0].scoring_basis == 'title only'


@pytest.mark.parametrize('failure_stage', ['corpus', 'ranking'])
def test_pending_delivery_survives_new_recommendation_failure(pipeline, monkeypatch, failure_stage):
    with open_dict(pipeline):
        pipeline.output.email.enabled = True
    previous = make_sample_paper(title='Previously selected', url='https://example.org/previous', score=8)
    state = State(pipeline.state.path)
    state.add([previous])
    state.save()
    executor = Executor(pipeline)
    def fail(*args):
        raise OSError('upstream unavailable')
    if failure_stage == 'corpus':
        monkeypatch.setattr(executor, 'fetch_zotero_corpus', fail)
    else:
        monkeypatch.setattr(executor.retrievers['arxiv'], 'retrieve_papers', lambda: [make_sample_paper()])
        monkeypatch.setattr(executor.reranker, 'rerank', fail)
    sent = []
    monkeypatch.setattr('zot2dailypaper.executor.send_email', lambda *args: sent.append(args))
    with pytest.raises((RuntimeError, OSError), match='upstream unavailable'):
        executor.run()
    assert len(sent) == 1
    assert not State(pipeline.state.path).pending('email')
    assert not State(pipeline.state.path).pending('rss')
    assert ET.parse(pipeline.output.rss.path).findtext('./channel/item/title') == 'Previously selected'


def test_existing_research_square_version_is_not_recommended_again(pipeline, monkeypatch):
    items = [{'data': {'title': 'Earlier title', 'DOI': '10.21203/rs.3.rs-123/v1',
                      'dateAdded': '2026-03-02T00:00:00Z', 'abstractNote': 'Useful abstract', 'collections': []}}]
    monkeypatch.setattr('zot2dailypaper.executor.zotero.Zotero', lambda *a, **kw: make_stub_zotero_client(items=items))
    executor = Executor(pipeline)
    paper = make_sample_paper(title='Revised title', doi='10.21203/rs.3.rs-123/v2', url='https://doi.org/10.21203/rs.3.rs-123/v2')
    monkeypatch.setattr(executor.retrievers['arxiv'], 'retrieve_papers', lambda: [paper])
    monkeypatch.setattr(executor.reranker, 'rerank', lambda *a: pytest.fail('Existing DOI identity must be excluded'))
    executor.run()
    assert not ET.parse(pipeline.output.rss.path).findall('./channel/item')


def test_partial_researchsquare_results_save_delivery_without_repeat(pipeline, monkeypatch):
    pipeline.output.email.enabled = True
    pipeline.executor.send_empty = False
    sent=[]
    monkeypatch.setattr('zot2dailypaper.executor.send_email',lambda *args:sent.append(args))
    paper=make_sample_paper(source='researchsquare')
    for _ in range(2):
        executor=Executor(pipeline)
        executor.retrievers={'researchsquare':SimpleNamespace(retrieve_papers=lambda:[paper], failures=['S4306402450: OpenAlex source HTTP 404'])}
        with pytest.raises(RuntimeError, match='researchsquare: incomplete retrieval'):
            executor.run()
    assert len(sent)==1
    state=State(pipeline.state.path)
    assert not state.pending('email') and not state.pending('rss')


def test_openalex_401_keeps_source_failure_visible_after_50_saved_summaries_are_delivered(pipeline, monkeypatch):
    from zot2dailypaper.retriever.openalex_researchsquare import get_json
    pipeline.output.email.enabled = True
    pipeline.output.rss.enabled = False
    pipeline.state.enabled = True
    pipeline.executor.send_empty = False
    pipeline.executor.max_paper_num = 50
    pipeline.executor.quotas = None
    papers = [make_sample_paper(title=f'Saved summary {index}', doi=f'10.5555/openalex-{index}',
                               url=f'https://example.org/openalex-{index}', source='journals',
                               tldr=f'这是第{index}篇已保存摘要。', tldr_status='generated') for index in range(50)]
    sent, synced, closed = [], [], []
    response = SimpleNamespace(status_code=401, close=lambda: closed.append(1))
    client = SimpleNamespace(get=lambda *args, **kwargs: response)

    def refused():
        return get_json(client, '/works', {'cursor': 'opaque-next-cursor'})

    monkeypatch.setattr('zot2dailypaper.executor.send_email', lambda config, html: sent.append(html))
    monkeypatch.setattr('zot2dailypaper.executor.sync_client', lambda _: SimpleNamespace(
        sync=lambda batch, state: synced.append(len(batch))))
    monkeypatch.setattr('zot2dailypaper.protocol.Paper.generate_tldr', lambda *args, **kwargs:
                        pytest.fail('Saved-summary regression must not call a model'))
    for _ in range(2):
        executor = Executor(pipeline)
        executor.retrievers = {'journals': SimpleNamespace(retrieve_papers=lambda: papers),
                               'researchsquare': SimpleNamespace(retrieve_papers=refused)}
        with pytest.raises(RuntimeError, match='researchsquare: OpenAlex access/budget error HTTP 401'):
            executor.run()
    state = State(pipeline.state.path)
    assert len(sent) == 1 and synced == [50] and len(closed) == 2
    assert not state.pending('email') and len(state.records) == 50
    assert all(record['paper']['tldr_status'] == 'generated' for record in state.records.values())
    assert all(paper.tldr in sent[0] for paper in papers)


def test_opt_in_recovery_reranks_before_selection_and_reuses_context(pipeline,monkeypatch):
    pipeline.executor.max_paper_num=1
    pipeline.abstracts.pre_rank_max_papers=2
    pipeline.abstracts.enabled=True
    executor=Executor(pipeline)
    papers=[make_sample_paper(title='Initially preferred',abstract='',doi='10.5555/first',url='https://example.org/first'),
            make_sample_paper(title='Better after abstract',abstract='',doi='10.5555/second',url='https://example.org/second')]
    monkeypatch.setattr(executor.retrievers['arxiv'],'retrieve_papers',lambda:papers)
    stages=[]
    def rank(items,corpus):
        stages.append('rank')
        for i,p in enumerate(items):
            p.score=9 if i==1 and p.abstract else 5-i
            p.scoring_basis='abstract' if p.abstract else 'title only'
        return sorted(items,key=lambda p:p.score,reverse=True)
    contexts=[]
    def recover(items,config,context=None):
        contexts.append(context)
        stages.append('recover')
        if len(contexts)==1:
            assert len(items)==2
            items[1].abstract='Verified complete abstract about the topic.'
    monkeypatch.setattr(executor.reranker,'rerank',rank)
    monkeypatch.setattr('zot2dailypaper.executor.recover_abstracts',recover)
    executor.run()
    saved=list(State(pipeline.state.path).records.values())
    assert len(saved)==1 and saved[0]['paper']['title']=='Better after abstract'
    assert saved[0]['paper']['scoring_basis']=='abstract'
    assert stages==['rank','recover','rank','recover']
    assert contexts[0] is contexts[1] and contexts[1].deadline is None


def test_postselection_recovery_never_redraws_random_group(pipeline,monkeypatch):
    from zot2dailypaper.selection import select_papers as real_select
    pipeline.executor.max_paper_num=7
    pipeline.executor.quotas={'journals':1,'preprints':1,'random':5}
    pipeline.abstracts.enabled=True
    executor=Executor(pipeline)
    papers=[make_sample_paper(title=f'Topic {i}',abstract='',doi=f'10.5555/{i}',
             source='journals' if i%2 else 'arxiv',url=f'https://example.org/{i}') for i in range(12)]
    monkeypatch.setattr(executor.retrievers['arxiv'],'retrieve_papers',lambda:papers)
    samples=[]
    class Rng:
        def sample(self,pool,count):
            samples.append([p.title for p in pool[:count]])
            return pool[:count]
    monkeypatch.setattr('zot2dailypaper.executor.select_papers',lambda ranked,quotas,pending:real_select(ranked,quotas,pending,rng=Rng()))
    def recover(items,config):
        for p in items:p.abstract='Verified recovered original abstract.'
    monkeypatch.setattr('zot2dailypaper.executor.recover_abstracts',recover)
    executor.run()
    saved=[r['paper'] for r in State(pipeline.state.path).records.values()]
    assert len(samples)==1 and len(samples[0])==5
    assert [p['title'] for p in saved if p['recommendation_group']=='random']==samples[0]
    assert all(p['score']==90 and p['missing_abstract_factor']==1 for p in saved)
    assert all(p['selection_score']==pytest.approx(82) for p in saved)


def test_chemrxiv_pipeline_library_history_quotas_and_random(pipeline,monkeypatch):
    from zot2dailypaper.identity import canonical_doi
    from tests.test_chemrxiv import item,transport,ranker
    with open_dict(pipeline):
        pipeline.interest_profile.keywords=['chemistry']
        pipeline.interest_profile.keyword_weight=0.3
        pipeline.interest_profile.zotero_weight=0.7
        pipeline.executor.source=['chemrxiv']
        pipeline.preprint_interests={'chemrxiv':{'enabled':True}}
        pipeline.executor.quotas={'journals':25,'preprints':15,'random':5}
        pipeline.executor.max_paper_num=45
        pipeline.abstracts.enabled=False
    seen=make_sample_paper(source='chemrxiv',doi='10.26434/chemrxiv.15000999/v1',title='Historic title')
    history=State(pipeline.state.path);history.add([seen]);history.mark([seen],'rss');history.save()
    records=[item(f'10.26434/chemrxiv.{15000000+i}/v1',title=[f'Candidate {i}']) for i in range(21)]
    records += [item('10.26434/chemrxiv.15000999/v2',title=['Changed historic title']),
        item('10.26434/chemrxiv.15000888/v1',relation={'is-preprint-of':[{'id':'10.1000/published','id-type':'doi'}]})]
    transport(monkeypatch,[{'message':{'items':records,'total-results':len(records)}}])
    executor=Executor(pipeline);executor.retrievers['chemrxiv']=ranker(pipeline)
    def corpus():
        executor.library_dois={canonical_doi('10.1000/published')}
        return []
    monkeypatch.setattr(executor,'fetch_zotero_corpus',corpus)
    def rank(papers,corpus):
        assert len(papers)==21
        for p in papers:p.score=8
        return papers
    monkeypatch.setattr(executor.reranker,'rerank',rank)
    monkeypatch.setattr('zot2dailypaper.executor.send_email',lambda *a:pytest.fail('No email'))
    monkeypatch.setattr('zot2dailypaper.executor.OpenAI',lambda **kw:pytest.fail('No LLM'))
    executor.run()
    saved=State(pipeline.state.path)
    added=[v['paper'] for k,v in saved.records.items() if k!='doi:10.26434/chemrxiv.15000999']
    assert len(added)==20 and len({p['doi'] for p in added})==20
    assert sum(p['recommendation_group']=='preprints' for p in added)==15
    assert sum(p['recommendation_group']=='random' for p in added)==5


def test_arxiv_503_preserves_other_delivery_and_never_resends(pipeline,monkeypatch):
    import arxiv
    pipeline.output.email.enabled=True
    pipeline.executor.send_empty=False
    sent=[]
    monkeypatch.setattr('zot2dailypaper.executor.send_email',lambda *args:sent.append(args))
    def fail():raise arxiv.HTTPError('https://export.arxiv.org/api/query',0,503)
    for _ in range(2):
        executor=Executor(pipeline)
        executor.retrievers={'arxiv':SimpleNamespace(retrieve_papers=fail),
            'journals':SimpleNamespace(retrieve_papers=lambda:[make_sample_paper(source='journals',doi='10.1000/other')])}
        with pytest.raises(RuntimeError,match='arxiv:.*HTTP 503'):executor.run()
    assert len(sent)==1
    state=State(pipeline.state.path)
    assert len(state.records)==1 and not state.pending('email')


def test_custom_fifty_paper_quota_is_not_truncated_by_legacy_maximum(pipeline,monkeypatch):
    pipeline.executor.quotas={'journals':25,'preprints':20,'random':5}
    pipeline.executor.max_paper_num=45
    executor=Executor(pipeline)
    papers=[make_sample_paper(title=f'Unique paper {i}',doi=f'10.8888/quota{i}',
             url=f'https://example.org/quota{i}',source='journals' if i<35 else 'arxiv') for i in range(70)]
    monkeypatch.setattr(executor.retrievers['arxiv'],'retrieve_papers',lambda:papers)
    executor.run()
    saved=[r['paper'] for r in State(pipeline.state.path).records.values()]
    assert len(saved)==50
    assert [sum(p['recommendation_group']==group for p in saved) for group in ('journals','preprints','random')]==[25,20,5]
    assert len({p['doi'] for p in saved})==50
