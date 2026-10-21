"""Table-based email cards inspired by TideDra/zotero-arxiv-daily."""
from .protocol import Paper
from .zotero_action import confirmation_origin, confirmation_link
from .budget import pricing_warning
from .selection import GROUPS, paper_group
from html import escape
from html.parser import HTMLParser
from urllib.parse import urlsplit, quote
from collections import Counter
import re


SUMMARY_REASONS = {
    'daily_budget_exhausted': 'daily AI budget reached',
    'daily_budget_reserved': 'daily AI budget already reserved by an earlier run or attempt',
    'legacy_budget_overlap': 'earlier UTC budget reservation overlaps this local budget day; reservation preserved',
    'budget_timezone_invalid': 'AI budget timezone configuration invalid',
    'budget_ledger_invalid': 'AI budget ledger or timestamps invalid; requests stopped',
    'day_changed': 'AI budget day changed',
    'budget_guard_unavailable': 'AI budget unavailable',
    'billing_unverified': 'AI billing verification failed',
    'model_unavailable': 'AI model unavailable',
    'request_failed': 'AI request failed',
    'request_timeout': 'AI request timed out',
    'connection_failed': 'AI connection failed',
    'provider_unavailable': 'AI provider temporarily unavailable',
    'authentication_failed': 'AI authentication rejected; requests stopped',
    'access_denied': 'AI access denied; no bypass attempted',
    'rate_limited': 'AI provider rate limited; requests stopped',
    'request_rejected': 'AI request rejected; requests stopped',
    'circuit_open': 'AI requests stopped after repeated temporary failures',
    'retry_wait_exceeded': 'AI provider retry delay exceeds the bounded wait; requests stopped',
    'retry_not_permitted': 'AI provider prohibited retries; requests stopped',
    'output_truncated': 'AI output truncated; original abstract retained',
    'content_filtered': 'AI output filtered',
    'empty_response': 'AI returned no summary text',
    'invalid_response': 'AI response invalid',
    'summary_language_mismatch': 'AI summary did not use the requested language',
    'summary_format_invalid': 'AI summary did not follow the single-sentence format',
    'client_setup_failed': 'AI client setup failed',
    'configuration_invalid': 'AI configuration invalid',
    'input_unavailable': 'no usable scientific summary input',
}


def summary_reason(paper):
    reason = SUMMARY_REASONS.get(paper.tldr_error_reason)
    if not reason and paper.tldr_error == 'budget_unavailable':
        reason = 'AI budget unavailable'
    if paper.tldr_error_reason == 'daily_budget_reserved' and re.fullmatch(r'\d{4}-\d{2}-\d{2}', paper.tldr_budget_day or ''):
        zone = 'Asia/Singapore' if paper.tldr_budget_timezone == 'Asia/Singapore' else 'UTC'
        reason += f' ({zone} {paper.tldr_budget_day}; reservation preserved)'
    return reason


def safe_url(value):
    try:
        parsed = urlsplit(value or '')
        return value if parsed.scheme in ('http', 'https') and parsed.netloc else ''
    except ValueError:
        return ''


def link(label, url, primary=False):
    url = safe_url(url)
    if not url:
        return ''
    color, background = ('#ffffff', '#b8423c') if primary else ('#334155', '#edf1f5')
    # Cell padding supplies a real gap even in email clients that ignore anchor margins.
    return ('<table class="paper-action" role="presentation" align="left" cellspacing="0" cellpadding="0" border="0" '
            'style="font-family:Aptos,Calibri,Arial,Helvetica,sans-serif;display:inline-table;">'
            '<tr><td style="font-family:Aptos,Calibri,Arial,Helvetica,sans-serif;padding:4px 20px 4px 0;">'
            f'<a href="{escape(url, quote=True)}" style="font-family:Aptos,Calibri,Arial,Helvetica,sans-serif;display:inline-block;'
            f'padding:10px 16px;border-radius:5px;background:{background};color:{color};'
            f'font-size:14px;font-weight:bold;text-decoration:none;">{escape(label)}</a></td></tr></table>')


framework = '''<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Daily Papers</title></head>
<body style="font-family:Aptos,Calibri,Arial,Helvetica,sans-serif;margin:0;padding:0;background:#eef1f4;color:#253244;">
<table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" bgcolor="#eef1f4" style="font-family:Aptos,Calibri,Arial,Helvetica,sans-serif;table-layout:fixed;"><tr><td align="center" style="font-family:Aptos,Calibri,Arial,Helvetica,sans-serif;padding:24px 12px;">
<!--[if mso]><table role="presentation" width="760" style="font-family:Aptos,Calibri,Arial,Helvetica,sans-serif;"><tr><td style="font-family:Aptos,Calibri,Arial,Helvetica,sans-serif;"><![endif]-->
<table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" style="font-family:Aptos,Calibri,Arial,Helvetica,sans-serif;max-width:760px;table-layout:fixed;">
<tr><td style="font-family:Aptos,Calibri,Arial,Helvetica,sans-serif;padding:24px;background:#24364b;border-top:4px solid #b8423c;color:#ffffff;">
<p style="font-family:Aptos,Calibri,Arial,Helvetica,sans-serif;margin:0 0 8px;font-size:11px;letter-spacing:2px;">ZOTERO · RESEARCH DIGEST</p>
<h1 style="font-family:Aptos,Calibri,Arial,Helvetica,sans-serif;margin:0;font-size:28px;line-height:1.2;">Daily Papers</h1>
<p style="font-family:Aptos,Calibri,Arial,Helvetica,sans-serif;margin:12px 0 0;font-size:14px;line-height:1.6;color:#dce5ee;">__COUNT__ · Journal and preprint selections ranked by relevance; random picks sampled from remaining eligible papers</p></td></tr>
<tr><td style="font-family:Aptos,Calibri,Arial,Helvetica,sans-serif;padding:16px 4px;font-size:12px;line-height:1.6;color:#526174;">__SCORING__ Scores range from 0 to 100. Higher scores mean a closer match, not a probability or accuracy estimate. Scores are shown to one decimal place.</td></tr>
__CONTENT__
<tr><td style="font-family:Aptos,Calibri,Arial,Helvetica,sans-serif;padding:16px 4px;font-size:12px;line-height:1.7;color:#526174;">zot2dailypaper · Based on Zotero-arXiv-Daily. To stop delivery, disable the scheduled workflow in GitHub Actions.</td></tr>
</table><!--[if mso]></td></tr></table><![endif]-->
</td></tr></table></body></html>'''


def get_block_html(title, authors, rate, tldr, pdf_url, affiliations=None, summary_label='AI summary',
                   *, number=None, metadata='', basis='abstract', article_url=None, doi=None, original_abstract=None, zotero_url=None, interest_reference="your library"):
    ordinal = f'{number}. ' if number is not None else ''
    heading = escape(ordinal + title)
    details = escape(metadata)
    buttons = link('PDF', pdf_url, True) + link('Article', article_url, not safe_url(pdf_url))
    buttons += link('保存到 Zotero', zotero_url)
    doi_url = 'https://doi.org/' + quote(doi, safe='/') if doi else None
    doi_line = (f'<p style="font-family:Aptos,Calibri,Arial,Helvetica,sans-serif;margin:10px 0 0;font-size:12px;overflow-wrap:anywhere;word-break:break-word;">'
                f'<a style="font-family:Aptos,Calibri,Arial,Helvetica,sans-serif;color:#526174;" href="{escape(doi_url, quote=True)}">DOI: {escape(doi)}</a></p>') if doi else ''
    # Retain the keyword for callers, but never duplicate an original below an AI summary.
    if summary_label != 'AI summary' and original_abstract is not None:
        tldr = original_abstract
    basis_note = (f'<br><span style="font-family:Aptos,Calibri,Arial,Helvetica,sans-serif;color:#687589;font-size:12px;">'
                  f'Based on {escape(basis)} (abstract unavailable).</span>') if basis != 'abstract' else ''
    return f'''<table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" bgcolor="#ffffff" style="font-family:Aptos,Calibri,Arial,Helvetica,sans-serif;table-layout:fixed;border:1px solid #dbe1e8;border-radius:8px;">
<tr><td style="font-family:Aptos,Calibri,Arial,Helvetica,sans-serif;padding:16px;overflow-wrap:anywhere;word-break:break-word;">
<p style="font-family:Aptos,Calibri,Arial,Helvetica,sans-serif;margin:0 0 10px;color:#687589;font-size:12px;line-height:1.6;">{details}</p>
<h2 style="font-family:Aptos,Calibri,Arial,Helvetica,sans-serif;margin:0 0 12px;color:#253244;font-size:18px;line-height:1.4;">{heading}</h2>
<p style="font-family:Aptos,Calibri,Arial,Helvetica,sans-serif;margin:0 0 5px;color:#526174;font-size:13px;line-height:1.7;">{escape(authors or 'Authors unavailable')}</p>
<p style="font-family:Aptos,Calibri,Arial,Helvetica,sans-serif;margin:0 0 16px;color:#687589;font-size:12px;line-height:1.6;">{escape(affiliations or 'Unknown Affiliation')}</p>
<p style="font-family:Aptos,Calibri,Arial,Helvetica,sans-serif;margin:0 0 18px;font-size:14px;line-height:1.7;color:#8e302c;"><strong>Relevance: {escape(str(rate))}/100</strong>{basis_note}</p>
<p style="font-family:Aptos,Calibri,Arial,Helvetica,sans-serif;margin:0 0 8px;font-size:12px;font-weight:bold;color:#526174;">{escape(summary_label)}</p>
<p style="font-family:Aptos,Calibri,Arial,Helvetica,sans-serif;margin:0 0 18px;font-size:15px;line-height:1.8;color:#334155;">{escape(tldr or 'No abstract available').replace(chr(10), '<br>')}</p>
<table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" style="font-family:Aptos,Calibri,Arial,Helvetica,sans-serif;"><tr><td style="font-family:Aptos,Calibri,Arial,Helvetica,sans-serif;">{buttons}</td></tr></table>{doi_line}
</td></tr></table>'''


def shorten_affiliations(affiliations, max_chars=180):
    if type(max_chars) is not int or not 20 <= max_chars <= 1000:
        raise ValueError('email.affiliation_max_chars must be an integer from 20 to 1000')
    text = ' '.join('; '.join(affiliations or []).split())
    return text if len(text) <= max_chars else text[:max_chars - 1].rstrip() + '…'


def email_summary(paper):
    if paper.has_ai_summary:
        return paper.tldr.strip(), 'AI summary'
    if paper.abstract:
        reason = summary_reason(paper)
        if paper.abstract_recovery_status in ('recovered_indexed_abstract', 'recovered_doi_linked_manuscript'):
            label = ('Indexed abstract (Semantic Scholar; version unverified)' if paper.abstract_recovery_status == 'recovered_indexed_abstract'
                     else f'Manuscript abstract ({paper.abstract_source})')
            return paper.abstract, label + (f' — {reason}' if reason else '')
        if paper.tldr_status == 'not_generated' and not paper.tldr_error:
            return paper.abstract, 'Original abstract (AI summary not generated)'
        if reason:
            return paper.abstract, f'Original abstract ({reason})'
        return paper.abstract, 'Original abstract (AI summary unavailable)'
    reasons = {
        'access_blocked': 'access blocked; no bypass attempted',
        'http_401': 'HTTP 401 access denied', 'http_403': 'HTTP 403 access denied',
        'http_429': 'HTTP 429 rate limited', 'not_found': 'metadata not found',
        'doi_version_mismatch': 'DOI/version mismatch; rejected',
        'title_mismatch': 'title mismatch; rejected',
        'abstract_absent': 'metadata contains no abstract',
        'correction_no_standalone_abstract': 'correction/erratum; checked metadata provides no standalone abstract',
        'abstract_incomplete': 'truncated abstract rejected',
        'abstract_absent_or_identity_mismatch': 'no usable abstract or identity mismatch',
        'abstract_absent_or_incomplete': 'abstract absent or incomplete',
        'doi_version_title_mismatch': 'DOI/version/title mismatch; rejected',
        'no_verified_abstract': 'no DOI-verified abstract found',
        'publisher_access_blocked': 'publisher access blocked; no bypass attempted',
        'publisher_lookup_limit': 'publisher lookup allowance exhausted',
        'publisher_abstract_absent': 'verified publisher page provides no standalone abstract (not a request failure)',
        'metadata_lookup_limit': 'metadata lookup allowance exhausted',
        'metadata_abstract_unavailable': 'public metadata has no usable abstract',
        'pre_rank_time_limit': 'pre-ranking lookup time allowance exhausted',
        'doi_unavailable': 'no DOI available for recovery',
    }
    details = []
    for attempt in paper.abstract_recovery_attempts:
        status = attempt.get('status', '')
        reason = reasons.get(status, ('request timed out' if 'Timeout' in status else 'lookup failed') if status.startswith('request_failed_') else status.replace('_', ' '))
        details.append(f"{attempt.get('provider', 'Metadata')}: {reason}")
    if paper.abstract_recovery_status and not details:
        details.append(reasons.get(paper.abstract_recovery_status, paper.abstract_recovery_status.replace('_', ' ')))
    if summary_reason(paper):
        details.append(summary_reason(paper))
    return 'No abstract available' + ('. ' + '; '.join(details) if details else ''), 'Abstract unavailable'


def percentage(weight):
    return f'{weight:.1%}'.replace('.0%', '%')


def scoring_explanation(papers, interest_weights=None, ranking_strategy=None):
    weights = Counter((round(p.interest_keyword_weight, 6), round(p.interest_zotero_weight, 6)) for p in papers)
    if not weights:
        weights[tuple(interest_weights or (0., 1.))] = 0
    if len(weights) == 1:
        keyword, library = next(iter(weights))
        parts = []
        if keyword:
            parts.append(f'keywords ({percentage(keyword)})')
        if library:
            parts.append(f'your library ({percentage(library)})')
        text = 'Scored using abstract similarity to ' + ' and '.join(parts) + '.'
    else:
        parts = [f'{percentage(keyword)}/{percentage(library)} ({count} papers)'
                 for (keyword, library), count in weights.items()]
        text = 'Scored using abstract similarity with effective keyword/library weights: ' + '; '.join(parts) + '.'
    strategies = Counter(p.ranking_strategy or 'legacy_mean' for p in papers)
    if not strategies:
        strategies[ranking_strategy or 'legacy_mean'] = 0
    if len(strategies) > 1:
        text += (' Ranking uses multi-interest profiles for '
                 f"{strategies['multi_interest_profile']} papers and legacy mean aggregation for "
                 f"{strategies['legacy_mean']} papers; stored scores retain their original strategy.")
    elif 'multi_interest_profile' in strategies:
        text += (' Multi-interest profile ranking uses the strongest joint keyword/library direction. '
                 'Each library profile weights references by when they were added and shrinks toward the global mean; '
                 'directions with too little support, or no direction anchors, use the global library mean.')
    else:
        text += ' Legacy mean ranking averages keywords and weights library references by when they were added.'
    if any(p.scoring_basis != 'abstract' for p in papers):
        text += ' Title similarity is used where an abstract is unavailable.'
    return text


def metadata_label(paper, field):
    status = getattr(paper, field + '_status')
    noun = 'Authors' if field == 'authors' else 'Affiliations'
    if paper.source == 'openreview' and status in ('not_public', 'anonymized'):
        return f'{noun} withheld by OpenReview'
    if status == 'access_blocked':
        return f'{noun} unavailable (metadata access blocked)'
    if status == 'not_provided':
        return f'{noun} not provided in public metadata'
    if status == 'doi_unavailable':
        return f'{noun} unavailable (no verified DOI)'
    if status == 'lookup_limit':
        return f'{noun} unavailable (metadata lookup allowance reached)'
    if status == 'lookup_failed':
        return f'{noun} unavailable (metadata lookup failed)'
    return 'Authors unavailable' if field == 'authors' else 'Unknown Affiliation'


def render_email(papers: list[Paper], *, affiliation_max_chars=180, zotero_action_origin=None,
                 interest_weights=None, ranking_strategy=None, pricing_notice=None) -> str:
    shorten_affiliations([], affiliation_max_chars)  # Validate even an empty digest.
    zotero_action_origin = confirmation_origin(zotero_action_origin)
    labels = {'journals': '期刊 / Journals', 'preprints': '预印本 / Preprints（含会议论文）', 'random': '随机推荐 / Random'}
    sections = []
    for group in GROUPS:
        selected = [p for p in papers if paper_group(p) == group]
        parts = []
        for number, p in enumerate(selected, 1):
            authors = p.authors if len(p.authors) <= 5 else p.authors[:3] + ['...'] + p.authors[-2:]
            affiliations = shorten_affiliations(p.affiliations, affiliation_max_chars)
            date = p.published.strftime('%Y-%m-%d') if p.published else 'Date unavailable'
            metadata = f'{p.journal or p.source} · {date} · Source: {p.source}'
            if p.publication_kind:
                metadata += f' · Type: {p.publication_kind} / {p.publication_status or "unverified"}'
            if group == 'random':
                metadata += ' · Random selection'
            if p.summary_input_source:
                metadata += f' · Summary input: {p.summary_input_source}'
                if p.summary_input_fallback:
                    metadata += f' ({p.summary_input_fallback})'
            if p.abstract_source:
                metadata += f' · Abstract: {p.abstract_source}'
            if p.ranking_strategy == 'multi_interest_profile' and p.matched_interest:
                metadata += f' · Matched interest: {p.matched_interest}'
                if p.interest_zotero_weight and not p.interest_direction_reliability:
                    metadata += ' (library support limited; global mean used)'
            if p.raw_score is not None and p.missing_abstract_factor < 1:
                metadata += f' · Missing-abstract factor: {p.missing_abstract_factor:g}; unadjusted relevance: {p.raw_score:.1f}/100'
            if p.selection_score is not None and p.score is not None and p.selection_score != p.score:
                metadata += f' · Selected at relevance {p.selection_score:.1f}/100; updated after abstract recovery'
            summary, summary_label = email_summary(p)
            block = get_block_html(p.title, ', '.join(authors) or metadata_label(p, 'authors'), round(p.score, 1) if p.score is not None else 'Unknown',
                                   summary, p.pdf_url, affiliations or metadata_label(p, 'affiliations'), summary_label, number=number,
                                   metadata=metadata, basis=p.scoring_basis, article_url=p.url, doi=p.doi,
                                   zotero_url=confirmation_link(p, zotero_action_origin))
            parts.append('<tr><td style="font-family:Aptos,Calibri,Arial,Helvetica,sans-serif;padding:0 0 16px;">' + block + '</td></tr>')
        if not parts:
            parts.append('<tr><td style="font-family:Aptos,Calibri,Arial,Helvetica,sans-serif;padding:16px;background:#ffffff;">No new recommendations in this group.</td></tr>')
        heading = f'<h2 style="font-family:Aptos,Calibri,Arial,Helvetica,sans-serif;margin:0 0 14px;font-size:18px;color:#24364b;">{labels[group]} ({len(selected)})</h2>'
        sections.append('<tr><td class="digest-section" width="100%" style="font-family:Aptos,Calibri,Arial,Helvetica,sans-serif;padding:0 0 24px;">' + heading +
                        '<table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" style="font-family:Aptos,Calibri,Arial,Helvetica,sans-serif;table-layout:fixed;">' + ''.join(parts) + '</table></td></tr>')
    content = ''.join(sections)
    degraded = [p for p in papers if p.tldr_error or p.tldr_error_reason not in (None, 'llm_disabled')
                or (p.tldr_status == 'generated' and not p.has_ai_summary)]
    if degraded:
        generated = sum(p.has_ai_summary for p in papers)
        reasons = Counter(summary_reason(p) or 'AI summary unavailable' for p in degraded)
        detail = '; '.join(f'{reason}: {count}' for reason, count in sorted(reasons.items()))
        content = ('<tr><td style="font-family:Aptos,Calibri,Arial,Helvetica,sans-serif;padding:16px;'
                   'background:#fff3cd;color:#713f12;font-size:14px;line-height:1.7;">'
                   '<strong>AI 摘要状态 / AI summary status</strong><br>'
                   + escape(f'{generated}/{len(papers)} generated. Original abstracts retained when available. {detail}')
                   + '</td></tr>') + content
    warning = pricing_warning() if pricing_notice is None else pricing_notice
    if warning:
        content = ('<tr><td style="font-family:Aptos,Calibri,Arial,Helvetica,sans-serif;padding:16px;'
                   'background:#fff3cd;color:#713f12;border:2px solid #b7791f;font-size:14px;line-height:1.7;">'
                   '<strong>LLM 费用提醒 / Pricing warning</strong><br>' + escape(warning) + '</td></tr>') + content
    count = f'{len(papers)} recommendation'  + ('' if len(papers) == 1 else 's')
    template = framework
    if any(p.missing_abstract_factor < 1 for p in papers):
        template = template.replace('Scores are shown to one decimal place.',
            'Missing-abstract adjustment is applied before conversion to 0-100: in the original signed domain, positive values multiply '
            'by the stated factor and negative values divide by it. A factor of zero yields 0/100. Scores are shown to one decimal place.')
    return template.replace('__COUNT__', count).replace('__SCORING__', escape(scoring_explanation(papers, interest_weights, ranking_strategy))).replace('__CONTENT__', content)


class _PlainEmail(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.hidden, self.href = [], 0, ''

    def handle_starttag(self, tag, attrs):
        if tag in ('head', 'script', 'style'):
            self.hidden += 1
        if self.hidden:
            return
        if tag in ('p', 'h1', 'h2', 'tr', 'br'):
            self.parts.append('\n')
        if tag == 'a':
            self.href = safe_url(dict(attrs).get('href'))

    def handle_endtag(self, tag):
        if tag in ('head', 'script', 'style'):
            self.hidden = max(0, self.hidden - 1)
            return
        if self.hidden:
            return
        if tag == 'a' and self.href:
            self.parts.append(f' ({self.href})')
            self.href = ''
        if tag in ('p', 'h1', 'h2', 'tr'):
            self.parts.append('\n')

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def email_plain_text(html):
    parser = _PlainEmail()
    parser.feed(html)
    return '\n\n'.join(line.strip() for line in ''.join(parser.parts).splitlines() if line.strip())
