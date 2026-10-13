"""Bounded official login recovery; provider bodies and credentials stay private."""
import time
import requests
from requests.adapters import HTTPAdapter
from urllib3.exceptions import InvalidHeader
from urllib3.util.retry import Retry

API = 'https://api2.openreview.net'
LOGIN_ATTEMPTS = 2
MAX_RETRY_WAIT = 10


def safe_error_category(response):
    """Return only fixed labels, never the provider's messages or identifiers."""
    try:
        data = response.json()
    except Exception:
        return 'non_json_error'
    if not isinstance(data, dict):
        return 'unclassified_error'
    errors = data.get('errors', [])
    entries = [data] + (errors[:5] if isinstance(errors, list) else [])
    messages = ' '.join(str(e.get('message', ''))[:2000].casefold() for e in entries if isinstance(e, dict))
    if 'expiresin' in messages:
        return 'login_expiry_parameter_rejected'
    if 'mfa' in messages or 'two-factor' in messages:
        return 'MFA_required'
    if 'activat' in messages or 'confirm your email' in messages:
        return 'account_activation_required'
    if any(term in messages for term in ('invalid credentials', 'invalid username', 'invalid password',
                                         'incorrect password', 'username or password', 'user not found')):
        return 'credentials_or_login_identifier_rejected'
    if 'email' in messages and any(term in messages for term in ('invalid', 'required', 'format')):
        return 'login_email_format_rejected'
    return 'unclassified_error'


class OpenReviewLoginError(RuntimeError):
    def __init__(self, category, attempts, http_status=None):
        self.category, self.attempts, self.http_status = category, attempts, http_status
        status = f'HTTP {http_status}; ' if http_status is not None else ''
        action = ('complete an officially supported account authentication setup' if category == 'MFA_required'
                  else 'verify credentials/account access' if category in {
                      'authentication_rejected', 'access_denied', 'credentials_or_login_identifier_rejected',
                      'account_activation_required', 'login_email_format_rejected'}
                  else 'inspect this safe category; retry only in a later authorized run')
        super().__init__(f'OpenReview official login failed ({status}category={category}; '
                         f'attempts={attempts}; response withheld); {action}')


def _retry_wait(response):
    header = getattr(response, 'headers', {}).get('Retry-After')
    try:
        delay = Retry().parse_retry_after(header) if header is not None else 2
    except (InvalidHeader, TypeError, ValueError, OverflowError):
        return None
    return max(2, delay) if delay <= MAX_RETRY_WAIT else None


def login(client, username, password):
    """At most two POSTs; refusals, redirects, MFA and malformed success stop."""
    # The shared GET adapter also retries connection errors for POST. Disable
    # those implicit attempts on this fixed endpoint so this bound is exact.
    mount = getattr(client, 'mount', None)
    if callable(mount):
        mount(API + '/login', HTTPAdapter(max_retries=0))
    for attempt in range(1, LOGIN_ATTEMPTS + 1):
        response, status, retryable, delay = None, None, False, 2
        category = 'client_error'
        try:
            response = client.post(API + '/login', json={'id': username, 'password': password, 'expiresIn': 3600},
                                   timeout=(10, 30), allow_redirects=False)
            status = response.status_code
            if type(status) is not int or not 100 <= status <= 599:
                status, category = None, 'invalid_response'
            elif status != 200:
                category = safe_error_category(response)
                if category in {'unclassified_error', 'non_json_error'}:
                    category = ('server_error' if status >= 500 else 'redirect_rejected' if 300 <= status < 400
                                else {401: 'authentication_rejected', 403: 'access_denied',
                                      429: 'rate_limited'}.get(status, 'request_rejected'))
                retryable = status in {500, 502, 503, 504} and category == 'server_error'
                if retryable:
                    delay = _retry_wait(response)
                    if delay is None:
                        category, retryable = 'retry_after_not_within_bound', False
            else:
                try:
                    data = response.json()
                except Exception:
                    data = None
                if not isinstance(data, dict):
                    category = 'invalid_response'
                elif data.get('mfaPending'):
                    category = 'MFA_required'
                else:
                    token = data.get('token')
                    if isinstance(token, str) and 0 < len(token) <= 8192 and all(33 <= ord(c) <= 126 for c in token):
                        return token
                    category = 'invalid_session_token'
        except (requests.exceptions.SSLError, requests.exceptions.ProxyError):
            category = 'TLS_or_proxy_error'
        except (requests.Timeout, requests.ConnectionError):
            category, retryable = 'temporary_transport_error', True
        except Exception:
            category = 'client_error'
        finally:
            close = getattr(response, 'close', None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass
        if not retryable or attempt == LOGIN_ATTEMPTS:
            raise OpenReviewLoginError(category, attempt, status) from None
        time.sleep(delay)
