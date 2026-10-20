"""Compose a local runtime configuration without exposing secret values in logs."""
import os
from pathlib import Path
import re

from omegaconf import OmegaConf
from hydra import compose, initialize_config_dir
from loguru import logger
from zot2dailypaper.preprint_interests import enabled_sources
from zot2dailypaper.recommendation_sync import WORKER_ORIGIN


def prepare(root: Path, environ=os.environ):
    scheduled = environ.get('GITHUB_EVENT_NAME') == 'schedule'
    name = 'interests' if scheduled else (environ.get('PAPER_CONFIG', '').strip() or 'all')
    if not re.fullmatch(r'[a-zA-Z0-9_-]+', name) or not (root / 'config' / f'{name}.yaml').is_file():
        raise ValueError('PAPER_CONFIG must name an existing configuration under config/')
    config = OmegaConf.create({'defaults': [name, '_self_']})
    custom = environ.get('CUSTOM_CONFIG', '')
    if custom.strip():
        supplied = OmegaConf.create(custom)
        if not OmegaConf.is_dict(supplied) or 'defaults' in supplied:
            raise ValueError('CUSTOM_CONFIG must be a YAML mapping without defaults')
        config = OmegaConf.merge(config, supplied)
    sync_mode = environ.get('ZOTERO_WORKER_SYNC', 'disabled')
    if sync_mode not in ('disabled', 'free'):
        raise ValueError('ZOTERO_WORKER_SYNC must be disabled or free')
    config = OmegaConf.merge(config, {'email': {'worker_sync': sync_mode}})
    if sync_mode == 'free':
        # Approved destination wins over a stale Site origin in CUSTOM_CONFIG.
        config = OmegaConf.merge(config, {'email': {'zotero_action_origin': WORKER_ORIGIN}})
    profile = 'interests' if scheduled else environ.get('PREPRINT_PROFILE', 'configured')
    if profile not in ('configured', 'interests'):
        raise ValueError('Invalid preprint profile')
    if profile == 'interests':
        preferences = OmegaConf.load(root / 'config' / 'interests.yaml').preprint_interests
        chemrxiv = OmegaConf.select(config, 'preprint_interests.chemrxiv')
        config.preprint_interests = preferences  # Replace legacy keyword approximations too.
        if chemrxiv is not None:
            config.preprint_interests.chemrxiv = OmegaConf.merge(preferences.chemrxiv, chemrxiv)
    sources = 'all' if scheduled else environ.get('SOURCE_MODE', 'configured')
    if sources not in ('configured', 'all', 'journals'):
        raise ValueError('Invalid sources mode')
    if sources != 'configured':
        selected = ['journals'] if sources == 'journals' else ['journals', 'arxiv', 'biorxiv', 'medrxiv', 'researchsquare', 'openreview', 'chemrxiv']
        config = OmegaConf.merge(config, {'executor': {'source': selected}})
    llm_mode = environ.get('LLM_MODE', 'configured')
    if llm_mode not in ('configured', 'disabled'):
        raise ValueError('Invalid llm_mode')
    # Keep the guard mandatory while preserving CUSTOM_CONFIG's budget amount.
    config = OmegaConf.merge(config, {'llm': {'budget': {'enabled': True}}})
    if llm_mode == 'disabled':
        config = OmegaConf.merge(config, {'llm': {'enabled': False}})
    channel = environ.get('OUTPUT_CHANNEL', 'configured')
    if channel not in ('configured', 'email', 'rss', 'both'):
        raise ValueError('Invalid output channel')
    # Repository delivery policy overrides stale CUSTOM_CONFIG and dispatch inputs.
    # RSS remains available to explicit local callers, not these email workflows.
    config = OmegaConf.merge(config, {'output': {'email': {'enabled': True},
                                               'rss': {'enabled': False}}})
    if scheduled:
        config = OmegaConf.merge(config, {'llm': {'language': 'Chinese'}, 'abstracts': {'enabled': True}})
        # Keep the daily destination in the existing secret, never in public code
        # or a stale CUSTOM_CONFIG receiver. Resolution happens only at delivery.
        config = OmegaConf.merge(config, {'email': {'receiver': '${oc.env:RECEIVER}'}})
    recipient = '' if scheduled else environ.get('RECEIVER_OVERRIDE', '').strip()
    if recipient:
        if not re.fullmatch(r'[^\s<>@,;]+@[^\s<>@,;]+\.[^\s<>@,;]+', recipient) or '${' in recipient:
            raise ValueError('recipient must be one plain email address')
        # An explicit per-run destination takes precedence over CUSTOM_CONFIG.
        config = OmegaConf.merge(config, {'email': {'receiver': recipient}})
    days = environ.get('WINDOW_DAYS', '')
    if days:
        if not days.isdigit() or not 1 <= int(days) <= 90:
            raise ValueError('window_days must be between 1 and 90')
        config = OmegaConf.merge(config, {'source': {name: {'window_days': int(days)}
                                                   for name in ('journals', 'arxiv', 'biorxiv', 'medrxiv', 'researchsquare', 'openreview', 'chemrxiv')}})
    # Workflow history lives in paper-state; never rely on an immutable cache key.
    config = OmegaConf.merge(config, {'state': {'enabled': True, 'path': 'data/recommendations.json'}})
    config = OmegaConf.merge(config, {'output': {'rss': {'path': 'public/feed.xml'}}})
    OmegaConf.save(config, root / 'config' / 'runtime.yaml')
    with initialize_config_dir(config_dir=str((root / 'config').resolve()), version_base=None):
        effective = compose(config_name='runtime')
    if scheduled:
        # CUSTOM_CONFIG historically set this to one rolling day, excluding
        # submissions first announced after moderation/weekend delays.
        arxiv_days = effective.source.arxiv.window_days
        if arxiv_days is not None and (type(arxiv_days) is not int or not 1 <= arxiv_days <= 90):
            raise ValueError('source.arxiv.window_days must be an integer between 1 and 90')
        if arxiv_days is None or arxiv_days < 7:
            logger.warning(f'Scheduled arXiv window widened from {arxiv_days} to 7 days for announcement delays')
            config = OmegaConf.merge(config, {'source': {'arxiv': {'window_days': 7}}})
            effective.source.arxiv.window_days = 7
    if sources == 'all':
        # Legacy presets leave unused platforms unconfigured. Supply defaults
        # only for those platforms; retain explicit categories/windows.
        for name in ('arxiv', 'biorxiv', 'medrxiv'):
            if effective.source[name].category is None:
                defaults = {'category': ['*']}
                if effective.source[name].window_days is None:
                    defaults['window_days'] = 7 if name == 'arxiv' else 1
                config = OmegaConf.merge(config, {'source': {name: defaults}})
        OmegaConf.save(config, root / 'config' / 'runtime.yaml')
    selected = enabled_sources(effective)
    if not selected:
        raise ValueError('No enabled sources remain after preprint interest filtering')
    config = OmegaConf.merge(config, {'executor': {'source': selected}})
    OmegaConf.save(config, root / 'config' / 'runtime.yaml')
    # Avoid redeploying an old restored feed during an email-only run.
    if not effective.output.rss.enabled:
        (root / 'public' / 'feed.xml').unlink(missing_ok=True)


if __name__ == '__main__':
    prepare(Path(__file__).resolve().parents[1])
