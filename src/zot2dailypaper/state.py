"""Atomic local recommendation history; transport success is tracked per channel."""
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

from .identity import paper_doi, paper_dois, paper_id, title_key
from .protocol import Paper
from .scores import upgrade_scores


def utcnow():
    return datetime.now(timezone.utc)


def paper_dict(paper):
    data = asdict(paper)
    data.pop('full_text')  # Keep state compact; never persist downloaded full texts.
    data['published'] = paper.published.isoformat() if paper.published else None
    if data['score'] is not None:
        data['score'] = float(data['score'])
    return data


def load_paper(data):
    data = upgrade_scores(data)
    data.setdefault('tldr_status', 'legacy' if data.get('tldr') else 'not_generated')
    if data.get('published'):
        data['published'] = datetime.fromisoformat(data['published'])
    return Paper(**data)


class State:
    def __init__(self, path, enabled=True, retention_days=90):
        if retention_days < 1:
            raise ValueError('State retention_days must be positive')
        self.path, self.enabled = Path(path), enabled
        self.records = {}
        if enabled and self.path.exists():
            data = json.loads(self.path.read_text(encoding='utf-8'))
            if data.get('version') != 1 or not isinstance(data.get('records'), dict):
                raise ValueError('Unsupported or corrupt recommendation state; preserve file and investigate')
            cutoff = utcnow() - timedelta(days=retention_days)
            for record in data['records'].values():
                if datetime.fromisoformat(record['added']) < cutoff:
                    continue
                # Rebuild keys when identity normalization changes. Pending deliveries
                # must still be markable without resending already delivered records.
                paper = load_paper(record['paper'])
                record = dict(record, paper=paper_dict(paper))
                key = paper_id(paper)
                if key in self.records:
                    channels = self.records[key].setdefault('channels', {})
                    for channel, delivered in record.get('channels', {}).items():
                        channels[channel] = channels.get(channel, False) or delivered
                else:
                    self.records[key] = record

        self.titles = {}
        self.dois = set()
        for record in self.records.values():
            paper = load_paper(record['paper'])
            self.dois.update(paper_dois(paper))
            self.titles.setdefault(title_key(paper.title), set()).add(paper_doi(paper))

    def has(self, paper):
        doi = paper_doi(paper)
        matches = self.titles.get(title_key(paper.title), set())
        return bool(paper_dois(paper) & self.dois) or paper_id(paper) in self.records or bool(matches and (not doi or None in matches or doi in matches))

    def pending(self, channel):
        return [load_paper(v['paper']) for v in self.records.values() if not v.get('channels', {}).get(channel)]

    def add(self, papers):
        for paper in papers:
            self.dois.update(paper_dois(paper))
            self.titles.setdefault(title_key(paper.title), set()).add(paper_doi(paper))
            self.records.setdefault(paper_id(paper), {'added': utcnow().isoformat(), 'paper': paper_dict(paper), 'channels': {}})

    def mark(self, papers, channel):
        for paper in papers:
            self.records[paper_id(paper)]['channels'][channel] = True

    def save(self):
        if self.enabled:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix('.tmp')
            temporary.write_text(json.dumps({'version': 1, 'records': self.records}, ensure_ascii=False), encoding='utf-8')
            temporary.replace(self.path)
