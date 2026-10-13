"""Shared verified HTTP transport with connection pooling and bounded retries."""
import time
from threading import Lock
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


class BoundedRetry(Retry):
    def get_backoff_time(self):
        return max(1.0, super().get_backoff_time())

    def get_retry_after(self, response):
        delay = super().get_retry_after(response)
        # Do not retry earlier than requested. Refuse impractically long waits
        # so a single source cannot occupy the whole scheduled job.
        if delay is not None and delay > 60:
            raise requests.exceptions.RetryError('Server Retry-After exceeds the 60-second per-attempt budget')
        return delay


class CrossrefAdapter(HTTPAdapter):
    """One shared in-flight Crossref request and at most one start/second."""
    _lock = Lock()
    _last_start = 0.0

    def send(self, request, **kwargs):
        with self._lock:
            delay = 1.0 - (time.monotonic() - CrossrefAdapter._last_start)
            if delay > 0:
                time.sleep(delay)
            CrossrefAdapter._last_start = time.monotonic()
            try:
                return super().send(request, **kwargs)
            finally:
                CrossrefAdapter._last_start = time.monotonic()


def session(mailto: str | None = None) -> requests.Session:
    client = requests.Session()
    client.headers['User-Agent'] = 'zot2dailypaper/1.0' + (f' (mailto:{mailto})' if mailto else '')
    retry = BoundedRetry(total=3, backoff_factor=1, status_forcelist=(429, 500, 502, 503, 504),
                  allowed_methods=frozenset({'GET'}), respect_retry_after_header=True)
    client.mount('https://', HTTPAdapter(max_retries=retry))
    crossref_retry = BoundedRetry(total=5, backoff_factor=2, backoff_max=30,
        status_forcelist=(429, 500, 502, 503, 504), allowed_methods=frozenset({'GET'}),
        respect_retry_after_header=True)
    client.mount('https://api.crossref.org/', CrossrefAdapter(max_retries=crossref_retry))
    return client


def abstract_session(mailto=None):
    """Recovery has no automatic retries; access refusals stop that provider."""
    from .publisher_abstracts import publisher_session
    client = publisher_session()
    if mailto:
        client.headers['User-Agent'] += f' (mailto:{mailto})'
    client.mount('https://api.crossref.org/', CrossrefAdapter(max_retries=0))
    return client
