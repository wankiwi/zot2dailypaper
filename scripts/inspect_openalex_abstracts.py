"""One authenticated metadata GET; persist aggregate abstract-field counts only."""
from collections import Counter
import hashlib
import json
import os
from pathlib import Path

from loguru import logger

from scripts.diagnose_openalex import AuditSession
from zot2dailypaper.retriever.openalex_researchsquare import OpenAlexResearchSquare, abstract_text, get_json


REF = 'refs/heads/diagnostics/openalex-abstract-fields-20261023'
FILTER = ('locations.source.id:S4306525896,type:preprint,'
          'topics.subfield.id:1303|1304|1606|1702|2500|2505|2704|3104,'
          'from_publication_date:2026-10-22,to_publication_date:2026-10-23')


class OneRequestSession(AuditSession):
    def send(self, request, **kwargs):
        if self.audit['requests']:
            raise RuntimeError('Only one authenticated field-inspection request is allowed')
        response = super().send(request, **kwargs)
        raw = bytearray()
        try:
            for chunk in response.iter_content(chunk_size=16384):
                raw.extend(chunk)
                if len(raw) > 2_000_000:
                    raise RuntimeError('Field-inspection response exceeds two-megabyte bound')
            response._content, response._content_consumed = bytes(raw), True
        finally:
            response.close()
        return response

    def get(self, url, **kwargs):
        kwargs['stream'] = True
        return super().get(url, **kwargs)


def field_counts(records):
    report = {}
    for field in ('abstract_inverted_index', 'abstract'):
        values = [record[field] for record in records if field in record]
        report[field] = {'present': len(values), 'null': sum(v is None for v in values),
                         'nonempty': sum(bool(v) for v in values),
                         'types': dict(Counter(type(v).__name__ for v in values))}
    converted, nonempty, excluded, index_lost, direct_ignored = 0, 0, 0, 0, 0
    for record in records:
        paper = OpenAlexResearchSquare.convert(record)
        if paper is None:
            excluded += 1
            continue
        converted += 1
        nonempty += bool(paper.abstract)
        index_lost += bool(record.get('abstract_inverted_index')) and not abstract_text(record.get('abstract_inverted_index'))
        direct_ignored += isinstance(record.get('abstract'), str) and bool(record['abstract'].strip()) and not paper.abstract
    report.update(converted=converted, converted_nonempty_abstracts=nonempty, conversion_exclusions=excluded,
                  nonempty_index_decoded_empty=index_lost, nonempty_direct_abstract_converted_empty=direct_ignored)
    return report


def main():
    if (os.environ.get('GITHUB_REPOSITORY') != 'wankiwi/zot2dailypaper'
            or os.environ.get('GITHUB_REF') != REF or os.environ.get('GITHUB_EVENT_NAME') != 'push'):
        raise RuntimeError('Use only the focused read-only field-inspection branch')
    key = os.environ.get('OPENALEX_API_KEY', '')
    if not key or any(os.environ.get(name) for name in ('OPENAI_API_KEY', 'SENDER_PASSWORD', 'ZOTERO_KEY', 'ZOTERO_WORKER_D1_TOKEN')):
        raise RuntimeError('Only the authorized OpenAlex credential is allowed')
    audit = {'requests': 0, 'phase': 'abstract_fields_only', 'pages': [], 'filter': None}
    try:
        with OneRequestSession(key, audit) as client:
            data = get_json(client, '/works', {'filter': FILTER, 'per_page': 100, 'cursor': '*'})
        records, total = data['results'], int(data['meta']['count'])
        report = {'source_commit': os.environ.get('GITHUB_SHA'), 'from': '2026-10-22', 'to': '2026-10-23',
            'requests': audit['requests'], 'returned': len(records), 'total': total,
            'current_query_complete': len(records) >= total, 'original_raw_snapshot_available': False,
            'identity_digest': hashlib.sha256('\n'.join(sorted(record['id'] for record in records)).encode()).hexdigest(),
            'fields': field_counts(records), 'mail_model_and_production_state_operations': 0}
    except Exception as error:
        logger.error(f'Focused field inspection failed ({type(error).__name__}); no retry')
        raise SystemExit(1) from None
    output = Path('artifacts/openalex-abstract-fields.json')
    output.parent.mkdir(exist_ok=True)
    output.write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps(report, sort_keys=True))


if __name__ == '__main__':
    main()
