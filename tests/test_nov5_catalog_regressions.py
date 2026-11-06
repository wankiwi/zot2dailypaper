"""Regressions from the 2026-11-05 scheduled retrieval logs."""
from dataclasses import replace
import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from zot2dailypaper.journals import CORE, NATURE, NATURE_ALIASES, Journal, selected_journals
from zot2dailypaper.retriever.journal_retriever import JournalRetriever


@pytest.mark.parametrize('legacy,canonical', NATURE_ALIASES.items())
def test_canonical_nature_feed_and_legacy_preset(legacy, canonical):
    expected = NATURE[canonical]
    assert expected.rss == f'https://www.nature.com/{canonical}.rss'
    assert legacy not in NATURE
    assert selected_journals({'presets': [legacy, canonical]}) == [expected]


@pytest.mark.parametrize('legacy,canonical', NATURE_ALIASES.items())
def test_cached_legacy_catalog_cannot_restore_duplicate_or_dead_feed(legacy, canonical):
    current = NATURE[canonical]
    stale = replace(current, id=legacy, rss=f'https://www.nature.com/{legacy}.rss')
    for discovered in [{legacy: stale}, {legacy: stale, canonical: current},
                       {canonical: current, legacy: stale}]:
        selected = selected_journals({'presets': ['nature_family', legacy]}, discovered)
        matches = [j for j in selected if j.title == current.title]
        assert matches == [current]
        assert len({j.title for j in selected}) == len(selected)


def test_discovered_slug_replaces_bootstrap_by_exact_title_only():
    changed = replace(NATURE['nchem'], id='new-chem', rss='https://www.nature.com/new-chem.rss')
    distinct = Journal('chem-news', 'Nature Chemistry News', rss='https://www.nature.com/chem-news.rss')
    selected = selected_journals({'presets': ['nature_family', 'nchem']},
                                {'new-chem': changed, 'chem-news': distinct})
    assert [j for j in selected if j.title == 'Nature Chemistry'] == [changed]
    assert distinct in selected


@pytest.mark.parametrize('canonical_cached', [False, True])
def test_catalog_keeps_cached_issns_when_slug_changes(config, tmp_path, canonical_cached):
    path = tmp_path / 'catalog.json'
    old_issns = ['2058-7546']
    entries = {'natenergy': old_issns}
    if canonical_cached:
        entries['nenergy'] = ['1234-567X']
    path.write_text(json.dumps({'issns': entries}))
    config.source.journals.catalog_cache = str(path)
    config.source.journals.presets = ['natenergy']
    retriever = JournalRetriever(config)
    journal, = retriever._catalog()
    assert journal.id == 'nenergy'
    assert journal.rss == 'https://www.nature.com/nenergy.rss'
    assert journal.issns == tuple(entries.get('nenergy', old_issns))
    assert retriever.cache['issns']['natenergy'] == old_issns


def test_science_advances_uses_publisher_journal_code():
    assert parse_qs(urlsplit(CORE['science_advances'].rss).query)['jc'] == ['sciadv']


@pytest.mark.parametrize('name', ['main.yml', 'validate-encrypted-cache.yml'])
def test_cache_actions_use_supported_node24_runtime(name):
    text = (Path(__file__).resolve().parents[1] / '.github' / 'workflows' / name).read_text()
    assert 'actions/cache/restore@v5' in text
    assert 'actions/cache/save@v5' in text
    assert 'actions/cache/restore@v4' not in text
    assert 'actions/cache/save@v4' not in text


@pytest.mark.parametrize('legacy,canonical', NATURE_ALIASES.items())
def test_legacy_custom_override_still_replaces_preset(legacy, canonical):
    custom = {'id': legacy, 'title': NATURE[canonical].title,
              'issns': ['1234-567X'], 'rss': 'https://example.org/custom.rss'}
    selected = selected_journals({'presets': [legacy, canonical], 'custom': [custom]})
    assert selected == [Journal(canonical, custom['title'], tuple(custom['issns']), custom['rss'])]


def test_future_slug_change_retains_normalized_title_presets_and_overrides():
    changed = Journal('new-chem', 'Nature  CHEMISTRY', rss='https://www.nature.com/new-chem.rss')
    discovered = {'new-chem': changed}
    assert selected_journals({'presets': ['nchem']}, discovered) == [changed]
    custom = {'id': 'nchem', 'title': 'Nature Chemistry', 'rss': 'https://example.org/custom.rss'}
    selected = selected_journals({'presets': ['nchem'], 'custom': [custom]}, discovered)
    assert selected == [Journal('new-chem', 'Nature Chemistry', rss=custom['rss'])]
