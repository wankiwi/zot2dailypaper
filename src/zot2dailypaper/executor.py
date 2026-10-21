from loguru import logger
from pyzotero import zotero
from omegaconf import DictConfig, ListConfig
from .utils import glob_match
from .retriever import get_retriever_cls
from .protocol import CorpusPaper
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor
from .identity import canonical_doi, paper_doi, paper_dois, title_key, deduplicate
from .state import State
from .output import write_rss
from .reranker import get_reranker_cls
from .interest_profile import interest_profile
from .construct_email import render_email
from .utils import send_email
from openai import OpenAI, DefaultHttpxClient
from .llm import ModelRequests, request_policy
from .budget import budget_plan, prepare_budget, BudgetUnavailable
from .preprint_interests import enabled_sources
from .scores import minimum_score
from .selection import quotas_for, select_papers, pending_batch, is_cover_title
from .abstracts import clean_abstract, recover_abstracts, RecoveryContext, recovery_shortlist
from time import monotonic
from collections import Counter
from .metadata import recover_metadata
from .recommendation_sync import sync_client


def normalize_path_patterns(patterns: list[str] | ListConfig | None, config_key: str) -> list[str] | None:
    if patterns is None:
        return None

    if not isinstance(patterns, (list, ListConfig)):
        raise TypeError(
            f"config.zotero.{config_key} must be a list of glob patterns or null, "
            'for example ["2026/survey/**"]. Single strings are not supported.'
        )

    if any(not isinstance(pattern, str) for pattern in patterns):
        raise TypeError(f"config.zotero.{config_key} must contain only glob pattern strings.")

    return list(patterns)


class Executor:
    def __init__(self, config: DictConfig):
        self.config = config
        if type(config.llm.get('enabled', True)) is not bool:
            raise ValueError('llm.enabled must be a YAML boolean, not a string or number')
        if config.llm.get('enabled', True):
            request_policy(config.llm)
        if config.llm.get("input_mode", "abstract") not in ("abstract", "full_text"):
            raise ValueError("llm.input_mode must be abstract or full_text")
        self.include_path_patterns = normalize_path_patterns(config.zotero.include_path, "include_path")
        self.ignore_path_patterns = normalize_path_patterns(config.zotero.ignore_path, "ignore_path")
        self.retrievers = {source: get_retriever_cls(source)(config) for source in enabled_sources(config)}
        if not self.retrievers:
            raise ValueError('No enabled sources remain after preprint interest configuration')
        self.reranker = get_reranker_cls(config.executor.reranker)(config)
        self.openai_client = None
        self.llm_http_client = None
        self.llm_blocked_reason = None
        self.llm_blocked_code = None
        self.llm_blocked_day = None
        self.llm_blocked_timezone = None
        self.llm_retry_at = None
        self.effective_interest_weights = None
        self.model_requests = ModelRequests()
        self.library_dois, self.library_titles, self.library_titles_without_doi = set(), set(), set()

    def fetch_zotero_corpus(self) -> list[CorpusPaper]:
        logger.info("Fetching zotero corpus")
        zot = zotero.Zotero(self.config.zotero.user_id, 'user', self.config.zotero.api_key)
        collections = {c['key']: c['data'] for c in zot.everything(zot.collections())}
        items = zot.everything(zot.items(itemType='conferencePaper || journalArticle || preprint'))
        self.library_dois, self.library_titles, self.library_titles_without_doi = set(), set(), set()
        paths = {}

        def collection_path(key, visiting=frozenset()):
            if key in paths:
                return paths[key]
            if key in visiting:
                raise ValueError("Cycle in Zotero collection hierarchy")
            data = collections.get(key)
            if not data:
                logger.warning(f"Zotero collection {key} no longer exists; omitting path")
                return ''
            parent = data.get('parentCollection')
            prefix = collection_path(parent, visiting | {key}) if parent else ''
            paths[key] = '/'.join(filter(None, [prefix, data.get('name', '')]))
            return paths[key]

        corpus = []
        for item in items:
            data = item.get('data', {})
            title = data.get('title', '')
            doi = canonical_doi(data.get('DOI')) or canonical_doi(data.get('url'))
            if doi:
                self.library_dois.add(doi)
            if title_key(title):
                self.library_titles.add(title_key(title))
                if not doi:
                    self.library_titles_without_doi.add(title_key(title))
            abstract = clean_abstract(data.get('abstractNote'))
            if not abstract.strip():
                continue
            try:
                added = datetime.fromisoformat(data['dateAdded'].replace('Z', '+00:00')).astimezone(timezone.utc)
            except (KeyError, TypeError, ValueError):
                logger.warning(f"Skipping Zotero item with invalid dateAdded: {item.get('key', 'unknown')}")
                continue
            item_paths = [collection_path(key) for key in data.get('collections', [])]
            corpus.append(CorpusPaper(title=title, abstract=abstract, added_date=added,
                                      paths=[p for p in item_paths if p], doi=doi))
        logger.info(f"Fetched {len(corpus)} Zotero papers with abstracts")
        return corpus

    def filter_corpus(self, corpus: list[CorpusPaper]) -> list[CorpusPaper]:
        if self.include_path_patterns:
            corpus = [c for c in corpus if any(glob_match(path, pattern) for path in c.paths for pattern in self.include_path_patterns)]
        if self.ignore_path_patterns:
            corpus = [c for c in corpus if not any(glob_match(path, pattern) for path in c.paths for pattern in self.ignore_path_patterns)]
        logger.info(f"Selected {len(corpus)} Zotero papers for the interest profile")
        return corpus

    def _enrich(self, paper):
        if self.config.llm.get('input_mode', 'abstract') == 'full_text' and self.openai_client:
            retriever = self.retrievers.get(paper.source)
            if retriever:
                try:
                    paper = retriever.enrich(paper)
                except Exception as exc:
                    logger.warning(f"Full text unavailable for {paper.url}: {exc}")
        if self.openai_client:
            if not paper.has_ai_summary:
                paper.generate_tldr(self.openai_client, self.config.llm, self.model_requests)
        elif not paper.has_ai_summary:
            paper.tldr_status, paper.tldr_error, paper.tldr_error_reason = 'not_generated', None, None
            paper.tldr_attempts = 0
            paper.tldr_budget_day = getattr(self, 'llm_blocked_day', None)
            paper.tldr_budget_timezone = getattr(self, 'llm_blocked_timezone', None)
            paper.tldr_retry_at = getattr(self, 'llm_retry_at', None)
            paper.tldr_error_reason = 'llm_disabled' if not self.config.llm.get('enabled', True) else 'input_unavailable'
            if getattr(self, 'llm_blocked_reason', None):
                paper.tldr = paper.abstract
                paper.tldr_status = 'fallback' if paper.abstract else 'not_generated'
                paper.tldr_error = 'budget_unavailable'
                paper.tldr_error_reason = getattr(self, 'llm_blocked_code', None) or 'budget_guard_unavailable'
        return paper

    def _close_llm(self):
        for client in (self.openai_client, self.llm_http_client):
            if client is not None and hasattr(client, 'close'):
                try:
                    client.close()
                except Exception as exc:
                    logger.warning(f'Cannot close LLM client ({type(exc).__name__})')
        self.openai_client = self.llm_http_client = None

    def run(self):
        output = self.config.get('output', {})
        email_cfg, rss_cfg = output.get('email', {}), output.get('rss', {})
        email_enabled, rss_enabled = email_cfg.get('enabled', True), rss_cfg.get('enabled', False)
        # Validate secure sync setup before retrieval or any budgeted enrichment.
        metadata_sync = sync_client(self.config.email) if email_enabled else None
        # Deliberately exclude recipient, model credentials, endpoints and custom YAML.
        logger.info(f'Effective configuration: sources={list(self.retrievers)}, '
                    f'llm_enabled={bool(self.config.llm.get("enabled", True))}, '
                    f'email_enabled={bool(email_enabled)}, rss_enabled={bool(rss_enabled)}')
        if not email_enabled and not rss_enabled:
            raise ValueError('Enable at least one output channel')
        state_cfg = self.config.get('state', {})
        state = State(state_cfg.get('path', 'data/recommendations.json'),
                      enabled=state_cfg.get('enabled', False) or rss_enabled,
                      retention_days=int(state_cfg.get('retention_days', 90)))
        if email_enabled:
            state.assert_email_delivery_safe()
        quotas = quotas_for(self.config.executor)
        maximum = sum(quotas.values()) if quotas is not None else int(self.config.executor.max_paper_num)
        workers = int(self.config.executor.get('enrichment_workers', 1))
        if maximum < 1 or not 1 <= workers <= 8:
            raise ValueError('max_paper_num must be positive and enrichment_workers must be between 1 and 8')
        errors = []
        self.model_requests = ModelRequests()
        self.llm_blocked_reason = self.llm_blocked_code = self.llm_blocked_day = self.llm_retry_at = None
        self.llm_blocked_timezone = None
        try:
            ranked = self._recommend(state, errors, maximum, workers)
        except Exception as exc:
            logger.error(f'Cannot prepare new recommendations: {exc}')
            errors.append(f'recommendations: {exc}')
            ranked = []
        finally:
            self._close_llm()
        state.add(ranked)
        state.save()  # Preserve pending deliveries before contacting transports.
        if email_enabled:
            pending = pending_batch(state.pending('email'), quotas, maximum)
            if pending or (self.config.executor.send_empty and not errors):
                try:
                    if metadata_sync is not None:
                        metadata_sync.sync(pending, state)
                        logger.info(f'Verified {len(pending)} recommendation citations in D1')
                    generated = sum(p.has_ai_summary for p in pending)
                    reasons = dict(Counter(p.tldr_error_reason or p.tldr_error or 'not_generated'
                                           for p in pending if not p.has_ai_summary))
                    logger.info(f'Email AI summaries: {generated} generated; {len(pending) - generated} unavailable; reasons={reasons}')
                    send_email(self.config, render_email(pending, affiliation_max_chars=self.config.email.get('affiliation_max_chars', 180),
                                                         zotero_action_origin=self.config.email.get('zotero_action_origin'),
                                                         interest_weights=self.effective_interest_weights,
                                                         ranking_strategy=self.config.reranker.get('strategy', 'multi_interest_profile')))
                    logger.info(f'SMTP accepted {len(pending)} recommendations')
                    state.mark(pending, 'email')
                    try:
                        state.save()
                    except OSError:
                        # SMTP already accepted: retry only the idempotent local
                        # receipt, never the transport. Persistent failure still
                        # surfaces; a crash/remote commit cannot be made atomic.
                        logger.warning('SMTP accepted; retrying local delivery history once after an I/O failure')
                        state.save()
                except Exception as exc:
                    errors.append(f'email: {exc}')
        if rss_enabled:
            try:
                path, emitted = write_rss(state, rss_cfg)
                logger.info(f'RSS wrote {len(emitted)} items to {path}')
                state.mark(emitted, 'rss')
                state.save()
            except Exception as exc:
                errors.append(f'rss: {exc}')
        if errors:
            raise RuntimeError('Pipeline completed with failures: ' + '; '.join(errors))
        logger.info('Recommendation outputs completed')

    def _recommend(self, state, errors, maximum, workers):
        corpus = self.filter_corpus(self.fetch_zotero_corpus())
        self.effective_interest_weights = interest_profile(self.config).effective_weights(bool(corpus))
        if not corpus:
            logger.warning('No Zotero papers with abstracts matched; ranking by configured keywords only')
        candidates = []
        for source, retriever in self.retrievers.items():
            try:
                candidates.extend(retriever.retrieve_papers())
                if getattr(retriever, 'failures', []):
                    errors.append(f"{source}: incomplete retrieval for {', '.join(retriever.failures)}")
            except Exception as exc:
                logger.error(f'Retrieval failed for {source}: {exc}')
                errors.append(f'{source}: {exc}')
        covers = sum(is_cover_title(p.title) for p in candidates)
        if covers:
            logger.info(f'Excluded {covers} publisher cover items before ranking and quotas')
        candidates = [p for p in candidates if not is_cover_title(p.title)]
        for paper in candidates:
            paper.abstract = clean_abstract(paper.abstract)
        unique = deduplicate(candidates)
        if self.config.executor.get('exclude_existing', True):
            unique = [p for p in unique if not (paper_dois(p) & self.library_dois)
                      and title_key(p.title) not in (self.library_titles_without_doi if paper_doi(p) else self.library_titles)]
        unique = [p for p in unique if not state.has(p)]
        logger.info(f'{len(candidates)} candidates, {len(unique)} new papers after deduplication')
        ranked = self.reranker.rerank(unique, corpus) if unique else []
        recovery_config = self.config.get('abstracts', {})
        pre_limit = recovery_config.get('pre_rank_max_papers', 0)
        pre_seconds = recovery_config.get('pre_rank_seconds', 90)
        if type(pre_limit) is not int or not 0 <= pre_limit <= recovery_config.get('max_papers', 50):
            raise ValueError('abstracts.pre_rank_max_papers must be between 0 and max_papers')
        if type(pre_seconds) is not int or not 1 <= pre_seconds <= 600:
            raise ValueError('abstracts.pre_rank_seconds must be an integer from 1 to 600')
        recovery_context = None
        if pre_limit and recovery_config.get('enabled', False):
            shortlist = recovery_shortlist(ranked, pre_limit)
            recovery_context = RecoveryContext(deadline=monotonic() + pre_seconds)
            recover_abstracts(shortlist, recovery_config, context=recovery_context)
            recovered = sum(bool(p.abstract) for p in shortlist)
            if recovered:
                ranked = self.reranker.rerank(unique, corpus)
            logger.info(f'Pre-ranking abstract recovery: {recovered}/{len(shortlist)} recovered; reranked before quotas and random sampling')
            recovery_context.deadline = None
        minimum = minimum_score(self.config.executor)
        ranked = [p for p in ranked if p.score >= minimum]
        quotas = quotas_for(self.config.executor)
        pending = state.pending('email') if self.config.get('output', {}).get('email', {}).get('enabled', True) else []
        ranked = select_papers(ranked, quotas, pending)[:sum(quotas.values()) if quotas is not None else maximum]
        # Freeze membership, including random draws, before delivery-only recovery.
        # Retain eligibility/selection scores when recovered abstracts change display scores.
        missing_at_selection = {id(p) for p in ranked if not p.abstract}
        for paper in ranked:
            paper.selection_score = paper.score
        if recovery_context is None:
            recover_abstracts(ranked, recovery_config)
        else:
            recover_abstracts(ranked, recovery_config, context=recovery_context)
        restored = [p for p in ranked if id(p) in missing_at_selection and p.abstract]
        if restored:
            # New abstract vectors are encoded, unchanged reference vectors remain cached.
            # This mutates scores/basis only; do not redraw random picks or refill quotas.
            self.reranker.rerank(restored, corpus)
        recover_metadata(ranked, self.config.get('metadata', {}), context=recovery_context)
        logger.info(f'Paper metadata: {sum(not p.authors for p in ranked)}/{len(ranked)} authors unavailable; '
                    f'{sum(not p.affiliations for p in ranked)}/{len(ranked)} affiliations unavailable; '
                    f'author statuses={dict(Counter(p.authors_status or "unknown" for p in ranked if not p.authors))}; '
                    f'affiliation statuses={dict(Counter(p.affiliations_status or "unknown" for p in ranked if not p.affiliations))}')
        eligible_input = any(isinstance(p.abstract, str) and p.abstract.strip() for p in ranked)
        if ranked and self.config.llm.get('enabled', True) and (eligible_input or self.config.llm.get('input_mode') == 'full_text'):
            try:
                policy = request_policy(self.config.llm)
                budget_plan(self.config.llm)  # Reject unverified destinations before resolving client credentials.
                # Constructing a client sends no request. Fail before reserving
                # an entire day when local credentials/client setup are invalid.
                self.llm_http_client = DefaultHttpxClient(follow_redirects=False)
                self.openai_client = OpenAI(api_key=self.config.llm.api.key, base_url=self.config.llm.api.base_url,
                                            max_retries=0, timeout=policy['timeout_seconds'], http_client=self.llm_http_client)
                self.model_requests = prepare_budget(self.config.llm)
            except BudgetUnavailable as exc:
                self._close_llm()
                self.llm_blocked_reason = str(exc)
                self.llm_blocked_code = exc.reason
                self.llm_blocked_day, self.llm_retry_at = exc.day, exc.retry_at
                self.llm_blocked_timezone = exc.timezone_name
                logger.warning(f'LLM budget guard: {exc}')
            except Exception as exc:
                self._close_llm()
                self.llm_blocked_reason, self.llm_blocked_code = 'Local LLM client setup failed', 'client_setup_failed'
                logger.warning(f'LLM client setup failed ({type(exc).__name__}); retaining original abstracts')
        # Fork-based PDF extraction must run outside worker threads.
        if workers > 1 and self.config.llm.get('input_mode', 'abstract') != 'full_text':
            with ThreadPoolExecutor(max_workers=workers) as pool:
                ranked = list(pool.map(self._enrich, ranked))
        else:
            ranked = [self._enrich(p) for p in ranked]
        failed = sum(bool(p.tldr_error) for p in ranked)
        generated = sum(p.has_ai_summary for p in ranked)
        not_generated = sum(p.tldr_status == 'not_generated' and not p.tldr_error for p in ranked)
        if failed:
            reasons = dict(Counter(p.tldr_error_reason or p.tldr_error for p in ranked if p.tldr_error))
            logger.warning(f'AI summary degradation: {failed}/{len(ranked)} unavailable; '
                           f'{generated} generated; original abstracts retained when available. '
                           f'Reasons={reasons}; model unavailable={self.model_requests.unavailable}')
        else:
            logger.info(f'AI summaries: {generated} generated; {not_generated} not generated')
        guard = getattr(self, 'model_requests', None)
        logger.info(f'LLM request attempts: {sum(p.tldr_attempts for p in ranked)}; '
                    f'budget day={getattr(guard, "day", None) or getattr(self, "llm_blocked_day", None)}; '
                    f'circuit={getattr(guard, "stop_reason", None) or "closed"}')
        return ranked
