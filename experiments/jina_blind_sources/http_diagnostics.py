"""Bounded, private source diagnostics; never retain credentials/query/body text."""
from contextlib import contextmanager
import hashlib
from importlib.metadata import version
from time import monotonic
from time import sleep
from threading import Lock, local
import math
from urllib.parse import parse_qs, urlsplit, urlencode

_LOCK = Lock()
_LAST_END = 0.0


def safe_retry_after(value):
    try:
        number = float(value)
        return number if math.isfinite(number) and 0 <= number <= 86400 else None
    except (TypeError, ValueError):
        return None


def request_shape(url):
    try:
        parts = urlsplit(str(url))
        query = parse_qs(parts.query, keep_blank_values=True)
        redacted = {k:v for k,v in query.items() if k.casefold() not in
                    {'api_key','key','token','access_token','password','email','mailto','signature','sig'}}
        search = query.get('search_query', [urlencode(sorted(redacted.items()), doseq=True)])[0]
        integer = lambda name: int(query[name][0]) if query.get(name, [''])[0].isdigit() else None
        # Paths are emitted only for the supported public API endpoint. Unknown
        # path segments may contain account identifiers, so retain no such text.
        allowed = {'export.arxiv.org': {'/api/query'}, 'api.crossref.org': {'/works'},
                   'api.openalex.org': {'/works'}, 'api2.openreview.net': {'/notes'},
                   'api.openreview.net': {'/notes'}}
        path = parts.path if parts.path in allowed.get(parts.hostname, set()) else None
        if parts.hostname == 'api.biorxiv.org':
            import re
            if re.fullmatch(r'/details/(?:biorxiv|medrxiv)/\d{4}-\d{2}-\d{2}/\d{4}-\d{2}-\d{2}/\d+', parts.path):
                path = parts.path
        return {'domain': parts.hostname, 'path': path, 'page_start': integer('start'),
                'page_size': integer('max_results'), 'query_bytes': len(search.encode()),
                'query_sha256': hashlib.sha256(search.encode()).hexdigest(),
                'request_sha256': hashlib.sha256((str(parts.hostname)+parts.path+'?'+urlencode(sorted(redacted.items()),doseq=True)).encode()).hexdigest(),
                'page_offset': integer('offset'), 'page_number': integer('page'),
                'url_bytes': len(str(url).encode())}
    except (TypeError, ValueError):
        return {'domain': None, 'path': None, 'page_start': None, 'page_size': None,
                'query_bytes': None, 'query_sha256': None, 'url_bytes': None}


def error_record(error):
    response = getattr(error, 'response', None)
    url = getattr(error, 'url', None)
    if not url and response is not None:
        url = getattr(response, 'url', None)
    status = getattr(error, 'status', None)
    if status is None and response is not None:
        status = getattr(response, 'status_code', None)
    return {'error_module': type(error).__module__, 'error_type': type(error).__name__,
            'http_status': status if isinstance(status, int) else None,
            'sdk_retry_index': getattr(error, 'retry', None), **request_shape(url)}


class Audit:
    def __init__(self, source, scope, cap=2000):
        self.source, self.scope, self.cap = source, scope, cap
        self.events = []
        self.dropped = 0
        self.started = monotonic()
        self.operations = {}
        self.context = local()
        self.lock = Lock()

    def add(self, **fields):
        event = {'source': self.source, 'scope': self.scope,
                 'operation_id':getattr(self.context,'operation_id',None), **fields}
        with self.lock:
            if len(self.events) < self.cap:
                self.events.append(event)
            else:
                self.dropped += 1

    def finish(self, papers=None, error=None):
        return {'source': self.source, 'scope': self.scope,
                'status': 'failed' if error is not None else 'empty' if not papers else 'success',
                'count': None if error is not None else len(papers),
                'elapsed_ms': round((monotonic()-self.started)*1000, 3),
                'error': error_record(error) if error is not None else None,
                'events': self.events, 'dropped_events': self.dropped,
                'adapter_operations': self.operations,
                'coverage_complete': False if error is not None else True if self.source == 'arxiv' else None,
                'sdk_version': version('arxiv') if self.source == 'arxiv' else None}


@contextmanager
def audited_arxiv(audit):
    """Instrument the original transport, preserving its refusal/retry policy."""
    from zot2dailypaper.retriever import arxiv_retriever as module
    original = module.ArxivSession

    class TracedSession(original):
        def __init__(self):
            super().__init__()
            self._audit_attempts = {}

        def send(self, request, **kwargs):
            global _LAST_END
            shape = request_shape(request.url)
            key = (shape['query_sha256'], shape['page_start'])
            self._audit_attempts[key] = self._audit_attempts.get(key, 0) + 1
            start = monotonic()
            try:
                # The SDK's per-client clock resets between common/expanded
                # queries. Preserve its 3-second politeness across clients too.
                with _LOCK:
                    pause = max(0.0, 3.0-(monotonic()-_LAST_END))
                    if pause:
                        sleep(pause)
                    try:
                        response = super().send(request, **kwargs)
                    finally:
                        _LAST_END = monotonic()
            except Exception as error:
                audit.add(phase='http_attempt', method=request.method,
                          transport_attempt=self._audit_attempts[key],
                          elapsed_ms=round((monotonic()-start)*1000, 3),
                          **shape, **{k: v for k, v in error_record(error).items() if k not in shape})
                raise
            try:
                redirect_domain = urlsplit(response.headers.get('Location', '')).hostname
            except ValueError:
                redirect_domain = None
            audit.add(phase='http_attempt', method=request.method,
                      transport_attempt=self._audit_attempts[key],
                      elapsed_ms=round((monotonic()-start)*1000, 3),
                      http_status=response.status_code,
                      retry_after_seconds=safe_retry_after(response.headers.get('Retry-After')),
                      redirect_domain=redirect_domain,
                      **shape)
            return response

        def _validate_page(self, response, url):
            before = len(self._entry_ids)
            try:
                super()._validate_page(response, url)
            except Exception as error:
                audit.add(phase='page_validation', status='failed', **request_shape(url),
                          error_module=type(error).__module__, error_type=type(error).__name__)
                raise
            audit.add(phase='page_validation', status='complete', **request_shape(url),
                      page_entries=len(self._entry_ids)-before, expected_total=self.expected_total)

    module.ArxivSession = TracedSession
    try:
        yield
    finally:
        module.ArxivSession = original


def collect(retriever, source, scope):
    audit = Audit(source, scope)
    prior_failures = getattr(retriever, 'failures', [])
    prior_values = list(prior_failures)
    prior_conversions = getattr(retriever, '_capture_conversion_failures', 0)
    try:
        if source == 'arxiv':
            with audited_arxiv(audit):
                papers = retriever.retrieve_papers()
        else:
            with audited_requests(audit), source_adapter_operations(retriever, source, audit):
                papers = retriever.retrieve_papers()
    except Exception as error:
        # Whole-source failures never become an apparently successful count=0.
        return [], audit.finish(error=error)
    record = audit.finish(papers=papers)
    current = getattr(retriever, 'failures', [])
    fresh = current[len(prior_values):] if current is prior_failures and list(current[:len(prior_values)]) == prior_values else current
    failures = len(fresh)
    record['reported_subsource_failures'] = failures
    conversion_failures = max(0,getattr(retriever, '_capture_conversion_failures', 0)-prior_conversions)
    record['reported_conversion_failures'] = conversion_failures
    unresolved, recovered = observed_outcomes(audit)
    record['unrecovered_observed_failures'] = unresolved
    record['recovered_observed_failures'] = recovered
    bad_operations = sum(value['status'] != 'complete' for value in audit.operations.values())
    if failures or conversion_failures or unresolved or bad_operations or audit.dropped:
        record['status'], record['coverage_complete'] = 'partial', False
        if not papers and (unresolved or bad_operations):
            record['status'], record['count'] = 'failed', None
    return papers, record


def observed_outcomes(audit):
    """Only this collect's events; a different successful page cannot clear one."""
    unresolved, recovered = [], []
    events = audit.events
    for i,event in enumerate(events):
        if event.get('phase') not in ('http_attempt','http_response'):
            continue
        status = event.get('http_status')
        if not (event.get('error_type') or isinstance(status,int) and status >= 300):
            continue
        operation = audit.operations.get(event.get('operation_id'))
        reason = None
        if operation and operation['status'] == 'complete':
            reason = 'existing_source_adapter_validated_completion_or_allowed_fallback'
        elif event.get('method') == 'GET' and event.get('request_sha256') and status not in (401,403):
            if any(later.get('request_sha256') == event['request_sha256']
                   and later.get('operation_id') == event.get('operation_id')
                   and isinstance(later.get('http_status'),int) and 200 <= later['http_status'] < 300
                   for later in events[i+1:]):
                reason = 'same_request_later_succeeded_under_existing_transport'
        item = {'event_index':i,'http_status':status,'operation_id':event.get('operation_id'),
                'request_sha256':event.get('request_sha256')}
        (recovered if reason else unresolved).append(dict(item, recovery=reason) if reason else item)
    return unresolved,recovered


@contextmanager
def source_adapter_operations(retriever, source, audit):
    """Use the pinned journal adapter's own validated completion/fallback result.

    A random later HTTP200 is never a fallback certificate. Journal _journal
    already distinguishes valid RSS/index fallbacks from failed date coverage;
    _catalog already validates cached/bootstrap fallback. Observe, do not fetch.
    """
    changed = []
    if source == 'journals':
        from zot2dailypaper.retriever.journal_retriever import JournalRetriever
        if isinstance(retriever,JournalRetriever):
            for name in ('_catalog','_journal'):
                original = getattr(retriever,name)
                def wrapped(*args,_name=name,_original=original,**kwargs):
                    with audit.lock:
                        op=f'op{len(audit.operations)+1:05}'
                        audit.operations[op]={'role':_name,'status':'unknown'}
                    before=getattr(audit.context,'operation_id',None)
                    audit.context.operation_id=op
                    try:
                        result=_original(*args,**kwargs)
                        complete = (isinstance(result,list) and bool(result)) if _name == '_catalog' else (
                            isinstance(result,tuple) and len(result)==3 and result[2] is False)
                        audit.operations[op]['status']='complete' if complete else 'failed'
                        return result
                    except Exception:
                        audit.operations[op]['status']='failed'
                        raise
                    finally:
                        audit.context.operation_id=before
                setattr(retriever,name,wrapped)
                changed.append((name,original))
    try:
        yield
    finally:
        for name,original in changed:
            setattr(retriever,name,original)


@contextmanager
def audited_requests(audit):
    """Observe original requests transports; never add retry or alternate routes."""
    import requests
    original = requests.Session.send
    def send(client, request, **kwargs):
        start = monotonic()
        shape = request_shape(request.url)
        try:
            response = original(client, request, **kwargs)
        except Exception as error:
            audit.add(phase='http_attempt', method=request.method,
                      elapsed_ms=round((monotonic()-start)*1000,3), **shape,
                      **{k:v for k,v in error_record(error).items() if k not in shape})
            raise
        history = getattr(getattr(response.raw, 'retries', None), 'history', None)
        audit.add(phase='http_response', method=request.method, **shape,
                  http_status=response.status_code,
                  elapsed_ms=round((monotonic()-start)*1000,3),
                  adapter_retry_count=len(history) if history is not None else None,
                  adapter_retry_statuses=[h.status for h in history] if history is not None else None,
                  retry_count_note='adapter history when available; inner attempts of other SDKs may be unknown')
        return response
    requests.Session.send = send
    try:
        yield
    finally:
        requests.Session.send = original
