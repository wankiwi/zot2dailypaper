"""Daily empty-result warnings must not change source success or delivery history."""
from datetime import datetime, timezone
import json
from types import SimpleNamespace

import pytest
from omegaconf import open_dict

from zot2dailypaper import arxiv_health as module


EMPTY = {'api_entries': 0, 'primary_category_kept': 0, 'converted': 0, 'keyword_kept': 0}
SCOPE = {'categories': ['cs.AI'], 'keywords': [], 'window_days': 7, 'include_cross_list': False}


@pytest.fixture
def health(config, monkeypatch, tmp_path):
    monkeypatch.setenv('GITHUB_EVENT_NAME', 'schedule')
    with open_dict(config):
        config.state.enabled = True
        config.state.path = str(tmp_path / 'recommendations.json')
    path = tmp_path / 'arxiv_health.json'
    warnings = []
    monkeypatch.setattr(module, 'logger', SimpleNamespace(info=lambda _: None, warning=warnings.append))
    def record(day, counts=EMPTY, scope=SCOPE):
        module.record_health(config, scope, counts, datetime(2026, 10, day, 22, tzinfo=timezone.utc))
        return json.loads(path.read_text())
    return SimpleNamespace(record=record, path=path, warnings=warnings, config=config)


def test_three_distinct_consecutive_zero_days_warn_without_rejecting_empty(health):
    assert health.record(14)['zero_days'] == 1
    assert health.record(14)['zero_days'] == 1
    assert health.record(15)['zero_days'] == 2
    assert health.warnings == []
    assert health.record(16)['zero_days'] == 3
    assert len(health.warnings) == 1 and '3 consecutive UTC days' in health.warnings[0]
    assert health.record(16)['zero_days'] == 3


def test_positive_observation_resets_streak_and_same_day_empty_cannot_erase_it(health):
    health.record(14)
    health.record(15)
    positive = dict(EMPTY, api_entries=5, primary_category_kept=3, converted=3, keyword_kept=1)
    assert health.record(16, positive)['zero_days'] == 0
    assert health.record(16)['zero_days'] == 0
    assert health.record(17)['zero_days'] == 1
    assert health.warnings == []


def test_gap_and_scope_change_restart_streak_without_storing_keywords(health):
    health.record(10)
    health.record(11)
    assert health.record(13)['zero_days'] == 1
    assert health.record(14)['zero_days'] == 2
    scope = dict(SCOPE, keywords=['private-interest-marker'])
    assert health.record(15, scope=scope)['zero_days'] == 1
    assert 'private-interest-marker' not in health.path.read_text()
    assert health.warnings == []


def test_keyword_filter_can_be_identified_in_zero_warning(health):
    counts = dict(EMPTY, api_entries=100, primary_category_kept=30, converted=30)
    for day in (14, 15, 16):
        health.record(day, counts)
    assert "'api_entries': 100" in health.warnings[0]
    assert "'keyword_kept': 0" in health.warnings[0]


@pytest.mark.parametrize('event,enabled,debug', [
    ('workflow_dispatch', True, False), ('', True, False), ('schedule', False, False), ('schedule', True, True),
])
def test_manual_disabled_and_debug_checks_do_not_advance_daily_history(health, monkeypatch, event, enabled, debug):
    health.record(14)
    before = health.path.read_bytes()
    monkeypatch.setenv('GITHUB_EVENT_NAME', event)
    health.config.state.enabled = enabled
    health.config.executor.debug = debug
    health.record(15)
    assert health.path.read_bytes() == before and health.warnings == []


@pytest.mark.parametrize('content', ['broken', '{}', '[]', '{"version":1,"scope":"x","zero_days":0,"day_had_candidates":false,"last_day":"bad"}'])
def test_corrupt_health_history_is_preserved_and_reported_without_failing_source(health, content):
    health.path.write_text(content)
    module.record_health(health.config, SCOPE, EMPTY, datetime(2026, 10, 15, tzinfo=timezone.utc))
    assert health.path.read_text() == content
    assert len(health.warnings) == 1 and 'streak unknown' in health.warnings[0]


def test_out_of_order_observation_cannot_overwrite_latest_health(health):
    health.record(16)
    before = health.path.read_bytes()
    health.record(15)
    assert health.path.read_bytes() == before
    assert 'streak unknown' in health.warnings[0]


def test_health_write_failure_is_diagnostic_only(health, monkeypatch):
    from pathlib import Path
    def denied(self, *args, **kwargs):
        raise OSError('synthetic health write failure')
    monkeypatch.setattr(Path, 'write_text', denied)
    module.record_health(health.config, SCOPE, EMPTY, datetime(2026, 10, 15, tzinfo=timezone.utc))
    assert not health.path.exists() and 'streak unknown' in health.warnings[0]


def test_conflicting_path_never_overwrites_delivery_history(health):
    health.path.write_text('delivery history at a conflicting custom path')
    health.config.state.path = str(health.path)
    module.record_health(health.config, SCOPE, EMPTY, datetime(2026, 10, 15, tzinfo=timezone.utc))
    assert health.path.read_text() == 'delivery history at a conflicting custom path'
    assert 'conflicts with delivery history' in health.warnings[0]
