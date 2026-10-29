from .scores import SCORE_SCHEMA
from dataclasses import dataclass, field
from typing import Optional, TypeVar
from datetime import datetime
import re
from time import sleep
from openai import OpenAI
from loguru import logger
from .llm import model_unavailable, SummaryUnavailable, failure_reason, request_policy, transient_failure, retry_after_seconds
from .budget import BudgetUnavailable, BudgetRequests, PROMPT_BYTES, SYSTEM_BYTES, MAX_OUTPUT_TOKENS, audit_response, no_retry_client, verify_paid_client
RawPaperItem = TypeVar('RawPaperItem')

@dataclass
class Paper:
    source: str
    title: str
    authors: list[str]
    abstract: str
    url: str
    pdf_url: Optional[str] = None
    full_text: Optional[str] = None
    tldr: Optional[str] = None
    affiliations: Optional[list[str]] = None
    score: Optional[float] = None
    score_schema: str = SCORE_SCHEMA
    raw_score: Optional[float] = None
    selection_score: Optional[float] = None
    missing_abstract_factor: float = 1.0
    doi: Optional[str] = None
    related_dois: list[str] = field(default_factory=list)
    journal: Optional[str] = None
    issns: list[str] = field(default_factory=list)
    published: Optional[datetime] = None
    publication_kind: Optional[str] = None
    publication_status: Optional[str] = None
    publication_venue: Optional[str] = None
    recommendation_group: Optional[str] = None
    abstract_source: Optional[str] = None
    abstract_source_url: Optional[str] = None
    abstract_recovery_status: Optional[str] = None
    abstract_recovery_attempts: list[dict[str, str]] = field(default_factory=list)
    subject_match_reason: Optional[str] = None
    scoring_basis: str = "abstract"
    keyword_score: Optional[float] = None
    zotero_score: Optional[float] = None
    interest_keyword_weight: float = 0.0
    interest_zotero_weight: float = 1.0
    ranking_strategy: Optional[str] = None # None denotes legacy stored papers.
    matched_interest: Optional[str] = None
    interest_direction_support: int = 0
    interest_direction_reliability: float = 0.0
    summary_input_source: Optional[str] = None
    summary_input_fallback: Optional[str] = None
    tldr_status: Optional[str] = None
    tldr_error: Optional[str] = None
    tldr_error_reason: Optional[str] = None
    tldr_attempts: int = 0
    tldr_budget_day: Optional[str] = None
    tldr_budget_timezone: Optional[str] = None
    tldr_retry_at: Optional[str] = None
    authors_status: Optional[str] = None
    affiliations_status: Optional[str] = None
    authors_source: Optional[str] = None
    authors_source_url: Optional[str] = None
    affiliations_source: Optional[str] = None
    affiliations_source_url: Optional[str] = None
    metadata_recovery_attempts: list[dict[str, str]] = field(default_factory=list)

    @property
    def has_ai_summary(self):
        return (self.tldr_status == 'generated' and not self.tldr_error and not self.tldr_error_reason
                and isinstance(self.tldr, str) and bool(self.tldr.strip()))

    @property
    def summary_label(self):
        if self.has_ai_summary:
            return 'AI summary'
        if self.tldr_status in ('fallback', 'generated') or self.tldr_error:
            return 'Original abstract (AI summary unavailable)'
        if self.tldr_status == 'legacy' or (self.tldr_status is None and self.tldr):
            return 'Summary (legacy; origin unknown)'
        return 'Original abstract (AI summary not generated)' if self.abstract else 'AI summary not generated'

    @property
    def summary_text(self):
        if self.has_ai_summary or self.tldr_status == 'legacy' or (self.tldr_status is None and self.tldr):
            return self.tldr or self.abstract or 'No abstract available'
        return self.abstract or 'No abstract available'


    def _generate_tldr_with_llm(self, openai_client:OpenAI,llm_params:dict, requests=None) -> str:
        if llm_params.get('enabled', True) is not True:
            raise SummaryUnavailable('llm_disabled' if llm_params.get('enabled') is False else 'configuration_invalid')
        if not isinstance(requests, BudgetRequests) or llm_params.get("budget", {}).get("enabled", True) is not True:
            raise BudgetUnavailable("A mandatory daily budget reservation is required")
        lang = llm_params.get('language', 'Chinese')
        mode = llm_params.get('input_mode', 'abstract')
        if mode not in ('abstract', 'full_text'):
            raise ValueError('llm.input_mode must be abstract or full_text')
        self.summary_input_source, self.summary_input_fallback = 'abstract', None
        policy = request_policy(llm_params)
        title = (self.title or '').encode('utf-8')[:128].decode('utf-8', errors='ignore')
        full_text = self.full_text.strip() if isinstance(self.full_text, str) else ''
        abstract = self.abstract.strip() if isinstance(self.abstract, str) else ''
        full_prompt = f'Title: {title}\nFull text:\n{full_text}'
        if mode == 'full_text':
            if not full_text:
                self.summary_input_fallback = 'full_text_unavailable'
            elif len(full_prompt.encode('utf-8')) > PROMPT_BYTES:
                self.summary_input_fallback = 'full_text_exceeds_budget_input_bound'
            else:
                self.summary_input_source = 'full_text'
        if self.summary_input_source == 'full_text':
            prompt = full_prompt  # Entire retrieved text, never a prefix labeled as full text.
        else:
            if self.summary_input_fallback:
                logger.warning(f'Summary input fallback to abstract: {self.summary_input_fallback}')
            if not abstract:
                raise SummaryUnavailable('input_unavailable')
            prompt = f'Title: {title}\nAbstract: {abstract}'
            prompt = prompt.encode('utf-8')[:PROMPT_BYTES].decode('utf-8', errors='ignore')
        system = (f'Return exactly one sentence in {lang} summarizing the scientific evidence. '
                  'Keep it under 60 words (or 80 Chinese characters). No heading, list, or invented claims.')
        kwargs = dict(llm_params.get('generation_kwargs', {}))
        if len(system.encode('utf-8')) > SYSTEM_BYTES:
            raise BudgetUnavailable('System prompt exceeds verified input bound')
        kwargs = {'model': kwargs['model'], 'max_tokens': MAX_OUTPUT_TOKENS, 'n': 1,
                  'extra_body': {'enable_thinking': False}}
        
        def operation():
            verify_paid_client(openai_client, kwargs['model'], requests)
            request_client = no_retry_client(openai_client, policy['timeout_seconds'])
            requests.check_window()  # Recheck after client setup, immediately before the paid boundary.
            self.tldr_attempts += 1
            with requests.api_costs.attempt(getattr(openai_client, 'base_url', requests.expected_endpoint),
                                            kwargs['model'], 'chat', schedule=requests.price_schedule,
                                            retry=attempt > 0) as cost:
                response = request_client.chat.completions.create(
                    messages=[
                        {
                            "role": "system",
                            "content": system,
                        },
                        {"role": "user", "content": prompt},
                    ],
                    **kwargs
                )
                cost.observe(response)
                audit_response(response, kwargs['model'])
                finish = getattr(response.choices[0], 'finish_reason', None)
                if finish != 'stop':
                    raise SummaryUnavailable({'length': 'output_truncated', 'content_filter': 'content_filtered'}.get(finish, 'invalid_response')
                                             if isinstance(finish, str) else 'invalid_response')
                tldr = response.choices[0].message.content
                if not isinstance(tldr, str) or not tldr.strip():
                    raise SummaryUnavailable('empty_response')
                if getattr(response.choices[0].message, 'tool_calls', None) or getattr(response.choices[0].message, 'refusal', None):
                    raise SummaryUnavailable('invalid_response')
                if str(lang).casefold() in ('chinese', '中文', 'zh', 'zh-cn') and not re.search(r'[\u4e00-\u9fff]', tldr):
                    raise SummaryUnavailable('summary_language_mismatch')
                if (len([part for part in re.split(r'[。！？!?]+', tldr) if part.strip()]) != 1
                        or re.search(r'(?m)^\s*(?:[-*#]\s|\d+[.)]\s)', tldr)):
                    raise SummaryUnavailable('summary_format_invalid')
                return tldr.strip()
        for attempt in range(policy['max_attempts']):
            try:
                # The durable whole-day claim stays consumed; EACH dispatch
                # (including ambiguous timeout retries) precharges another slot.
                return requests.call(operation)
            except Exception as exc:
                if requests.stop_reason == 'retry_wait_exceeded':
                    raise SummaryUnavailable('retry_wait_exceeded') from None
                if requests.stop_reason == 'retry_not_permitted':
                    raise SummaryUnavailable('retry_not_permitted') from None
                if (not transient_failure(exc) or attempt + 1 == policy['max_attempts']
                        or requests.stop_reason or requests.unavailable):
                    raise
                wait = max(policy['retry_backoff_seconds'] * (2 ** attempt), retry_after_seconds(exc))
                if wait > policy['max_retry_wait_seconds']:
                    requests.stop('retry_wait_exceeded')
                    raise SummaryUnavailable('retry_wait_exceeded') from None
                sleep(wait)

    def generate_tldr(self, openai_client:OpenAI,llm_params:dict, requests=None) -> str:
        self.tldr_error, self.tldr_error_reason = None, None
        self.tldr_attempts, self.tldr_retry_at = 0, None
        self.tldr_budget_day = getattr(requests, 'day', None)
        self.tldr_budget_timezone = getattr(requests, 'timezone_name', None)
        if llm_params.get('enabled', True) is False:
            self.tldr, self.tldr_status, self.tldr_error_reason = '', 'not_generated', 'llm_disabled'
            return self.tldr
        has_abstract = isinstance(self.abstract, str) and bool(self.abstract.strip())
        has_full_text = isinstance(self.full_text, str) and bool(self.full_text.strip())
        if not has_abstract and (llm_params.get('input_mode', 'abstract') != 'full_text' or not has_full_text):
            self.tldr, self.tldr_status = '', 'not_generated'
            self.tldr_error_reason = 'input_unavailable'
            return self.tldr
        try:
            if type(llm_params.get('enabled', True)) is not bool:
                raise SummaryUnavailable('configuration_invalid')
            tldr = self._generate_tldr_with_llm(openai_client,llm_params,requests)
            self.tldr = tldr
            self.tldr_status = 'generated'
            return tldr.strip()
        except Exception as e:
            self.tldr_error = 'budget_unavailable' if isinstance(e, BudgetUnavailable) else 'model_unavailable' if model_unavailable(e) else 'request_failed'
            self.tldr_error_reason = e.reason if isinstance(e, BudgetUnavailable) else failure_reason(e)
            # Do not log provider response bodies, account IDs or request payloads.
            if requests is None:
                logger.warning(f'AI summary unavailable ({self.tldr_error}); using original abstract when available')
            tldr = self.abstract if has_abstract else ''
            self.tldr = tldr
            self.tldr_status = 'fallback' if has_abstract else 'not_generated'
            return tldr.strip()

    def generate_affiliations(self, openai_client:OpenAI, llm_params:dict, requests=None) -> Optional[list[str]]:
        """Compatibility API: retain metadata; optional paid extraction is disabled."""
        return self.affiliations

@dataclass
class CorpusPaper:
    title: str
    abstract: str
    added_date: datetime
    paths: list[str]
    doi: Optional[str] = None
