"""Focused synthetic checks; no live providers, models, delivery or production state."""
from types import SimpleNamespace

import pytest

from scripts.inspect_openalex_abstracts import field_counts
from tests.retriever.test_openalex_researchsquare import item
from tests.test_pipeline import pipeline  # noqa: F401
from zot2dailypaper.executor import Executor
from zot2dailypaper.retriever.openalex_researchsquare import OpenAlexResearchSquare
from zot2dailypaper.state import State


def test_source_field_presence_is_distinct_from_conversion_and_direct_field_support():
    records = [item(abstract_inverted_index=None), item(2, abstract_inverted_index={'water': [1], 'Molecular': [0]}),
               item(3, abstract='Direct abstract without an inverted index')]
    records[2].pop('abstract_inverted_index')
    stats = field_counts(records)
    assert stats['abstract_inverted_index'] == {'present': 2, 'null': 1, 'nonempty': 1, 'types': {'NoneType': 1, 'dict': 1}}
    assert stats['abstract']['present'] == stats['abstract']['nonempty'] == 1
    assert stats['converted'] == 3 and stats['converted_nonempty_abstracts'] == 1
    assert stats['nonempty_index_decoded_empty'] == 0 and stats['nonempty_direct_abstract_converted_empty'] == 1
    assert OpenAlexResearchSquare.convert(records[1]).abstract == 'Molecular water'


def test_title_only_researchsquare_can_be_selected_without_summary_or_model_call(pipeline, monkeypatch):
    pipeline.llm.enabled = True
    pipeline.llm.input_mode = 'abstract'
    pipeline.executor.max_paper_num = 1
    pipeline.executor.quotas = None
    pipeline.abstracts.enabled = True
    pipeline.abstracts.pre_rank_max_papers = 1
    paper = OpenAlexResearchSquare.convert(item(abstract_inverted_index=None))
    recoveries = []

    def unavailable(papers, config, context=None):
        recoveries.append(len(papers))
        for candidate in papers:
            candidate.abstract_recovery_status = 'researchsquare_abstract_unavailable'

    def forbidden(*args, **kwargs):
        pytest.fail('No model, SMTP or external state boundary may be reached')

    monkeypatch.setattr('zot2dailypaper.executor.recover_abstracts', unavailable)
    monkeypatch.setattr('zot2dailypaper.executor.OpenAI', forbidden)
    monkeypatch.setattr('zot2dailypaper.executor.prepare_budget', forbidden)
    monkeypatch.setattr('zot2dailypaper.executor.send_email', forbidden)
    executor = Executor(pipeline)
    executor.retrievers = {'researchsquare': SimpleNamespace(retrieve_papers=lambda: [paper])}
    errors = []
    selected = executor._recommend(State(pipeline.state.path), errors, 1, 1)
    assert selected == [paper] and errors == [] and recoveries == [1, 1]
    assert paper.scoring_basis == 'title only' and paper.missing_abstract_factor == .8
    assert paper.score >= 0 and paper.abstract == ''
    assert paper.tldr_status == 'not_generated' and paper.tldr_error_reason == 'input_unavailable'
    assert paper.tldr_attempts == 0 and not paper.has_ai_summary
