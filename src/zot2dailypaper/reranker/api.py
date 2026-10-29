from .base import BaseReranker, register_reranker
from openai import OpenAI, DefaultHttpxClient
import numpy as np
from ..api_costs import ApiCosts


@register_reranker('api')
class ApiReranker(BaseReranker):
    def _request_cost(self, request):
        # Observe every physical SDK dispatch while preserving its existing retry
        # policy. Retry metadata is numeric; headers/payloads are never retained.
        count = request.headers.get('x-stainless-retry-count', '0')
        event = self.api_costs.start_attempt(str(request.url),
            self.config.reranker.api.model, 'embedding', retry=count.isdigit() and int(count) > 0)
        event.failed = True  # A timeout/connection failure has no response hook.
        request.extensions['zot2dailypaper_cost'] = event
        self._last_embedding_attempt = event

    def _response_cost(self, response):
        event = response.request.extensions.get('zot2dailypaper_cost')
        if event is None:
            return
        # Embeddings are non-streaming. HTTPX would read this same body directly
        # after response hooks; subsequent SDK reads use the cached bytes.
        response.read()
        try:
            event.observe(response.json())
        except (ValueError, TypeError):
            pass  # Preserve the SDK's normal handling of a non-JSON response.
        event.failed = not response.is_success

    def get_similarity_score(self, s1: list[str], s2: list[str]) -> np.ndarray:
        if not s1 or not s2:
            return np.empty((len(s1), len(s2)))
        if not hasattr(self, 'api_costs'):
            self.api_costs = ApiCosts()
        if not hasattr(self, '_client'):
            http_client = DefaultHttpxClient(event_hooks={'request': [self._request_cost],
                                                        'response': [self._response_cost]})
            self._client = OpenAI(api_key=self.config.reranker.api.key, base_url=self.config.reranker.api.base_url,
                                  http_client=http_client)
            self._embeddings = {}
        batch_size = int(self.config.reranker.api.get('batch_size') or 64)
        if batch_size < 1:
            raise ValueError('Embedding batch_size must be positive')
        texts = list(dict.fromkeys(t for t in s1 + s2 if t not in self._embeddings))
        for i in range(0, len(texts), batch_size):
            batch = texts[i:i + batch_size]
            self._last_embedding_attempt = None
            try:
                response = self._client.embeddings.create(input=batch, model=self.config.reranker.api.model)
                # Embedding response order is not guaranteed; respect the supplied index.
                ordered = sorted(response.data, key=lambda r: r.index)
                if [r.index for r in ordered] != list(range(len(batch))):
                    raise ValueError('Embedding API returned incomplete or invalid indices')
                for text, result in zip(batch, ordered):
                    vector = np.asarray(result.embedding, dtype=float)
                    norm = np.linalg.norm(vector)
                    if vector.ndim != 1 or not np.isfinite(vector).all() or not norm:
                        raise ValueError('Embedding API returned an invalid or zero vector')
                    self._embeddings[text] = vector / norm
            except Exception:
                if self._last_embedding_attempt is not None:
                    self._last_embedding_attempt.failed = True
                raise
        left = np.stack([self._embeddings[t] for t in s1])
        right = np.stack([self._embeddings[t] for t in s2])
        return left @ right.T
