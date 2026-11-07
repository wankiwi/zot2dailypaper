"""Journal presets. Nature coverage is refreshed from its official site index."""
from dataclasses import dataclass, replace
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit
import re


@dataclass(frozen=True)
class Journal:
    id: str
    title: str
    issns: tuple[str, ...] = ()
    rss: str | None = None

    def __post_init__(self):
        for issn in self.issns:
            if not re.fullmatch(r'\d{4}-\d{3}[\dX]', issn):
                raise ValueError(f'Invalid ISSN for {self.title}: {issn}')
        if self.rss and urlsplit(self.rss).scheme != 'https':
            raise ValueError(f'Journal feeds must use HTTPS: {self.title}')


# ACS RSS endpoints reject automated requests (HTTP 403). Use the existing
# public Crossref journal source rather than repeatedly requesting blocked feeds.
CORE = {
    'pnas': Journal('pnas', 'Proceedings of the National Academy of Sciences of the United States of America', ('0027-8424', '1091-6490'), None),
    'acs_catalysis': Journal('acs_catalysis', 'ACS Catalysis', ('2155-5435',), None),
    'npjcompumats': Journal('npjcompumats', 'npj Computational Materials', ('2057-3960',), None),
    'angew': Journal('angew', 'Angewandte Chemie International Edition', ('1433-7851', '1521-3773'), None),
    'chemical_science': Journal('chemical_science', 'Chemical Science', ('2041-6520', '2041-6539'), None),
    'mlst': Journal('mlst', 'Machine Learning: Science and Technology', ('2632-2153',), None),
    'jacs': Journal('jacs', 'Journal of the American Chemical Society', ('0002-7863', '1520-5126'), None),
    'jctc': Journal('jctc', 'Journal of Chemical Theory and Computation', ('1549-9618', '1549-9626'), None),
    'prl': Journal('prl', 'Physical Review Letters', ('0031-9007', '1079-7114'), 'https://feeds.aps.org/rss/recent/prl.xml'),
    'nature': Journal('nature', 'Nature', ('0028-0836', '1476-4687'), 'https://www.nature.com/nature.rss'),
    'science': Journal('science', 'Science', ('0036-8075', '1095-9203'), 'https://www.science.org/action/showFeed?type=etoc&feed=rss&jc=science'),
    'science_advances': Journal('science_advances', 'Science Advances', ('2375-2548',), 'https://www.science.org/action/showFeed?type=etoc&feed=rss&jc=sciadv'),
    'jcp': Journal('jcp', 'The Journal of Chemical Physics', ('0021-9606', '1089-7690'), None),
    'jpcl': Journal('jpcl', 'The Journal of Physical Chemistry Letters', ('1948-7185',), None),
}
# Offline bootstrap list; live discovery adds new Nature-branded journals.
_NATURE = {
    'nature': 'Nature', 'nataging': 'Nature Aging', 'natastron': 'Nature Astronomy',
    'natbiomedeng': 'Nature Biomedical Engineering', 'nbt': 'Nature Biotechnology',
    'natcardiovascres': 'Nature Cardiovascular Research', 'natcatal': 'Nature Catalysis',
    'ncb': 'Nature Cell Biology', 'nchembio': 'Nature Chemical Biology',
    'natchemeng': 'Nature Chemical Engineering', 'nchem': 'Nature Chemistry',
    'natcities': 'Nature Cities', 'nclimate': 'Nature Climate Change',
    'ncomms': 'Nature Communications', 'natcomputsci': 'Nature Computational Science',
    'natecolevol': 'Nature Ecology & Evolution', 'natelectron': 'Nature Electronics',
    'nenergy': 'Nature Energy', 'natfood': 'Nature Food', 'ng': 'Nature Genetics',
    'ngeo': 'Nature Geoscience', 'nathumbehav': 'Nature Human Behaviour',
    'ni': 'Nature Immunology', 'natmachintell': 'Nature Machine Intelligence',
    'nmat': 'Nature Materials', 'nm': 'Nature Medicine', 'natmentalhealth': 'Nature Mental Health',
    'natmetab': 'Nature Metabolism', 'nmeth': 'Nature Methods', 'nmicrobiol': 'Nature Microbiology',
    'nnano': 'Nature Nanotechnology', 'neuro': 'Nature Neuroscience', 'nphoton': 'Nature Photonics',
    'nphys': 'Nature Physics', 'nplants': 'Nature Plants', 'nprot': 'Nature Protocols',
    'nsmb': 'Nature Structural & Molecular Biology', 'natsustain': 'Nature Sustainability',
    'natsynth': 'Nature Synthesis', 'natwater': 'Nature Water',
    'natrevbioeng': 'Nature Reviews Bioengineering', 'nrc': 'Nature Reviews Cancer',
    'nrcardio': 'Nature Reviews Cardiology', 'natrevchem': 'Nature Reviews Chemistry',
    'nrclinonc': 'Nature Reviews Clinical Oncology', 'nrdp': 'Nature Reviews Disease Primers',
    'nrd': 'Nature Reviews Drug Discovery', 'natrevearthenviron': 'Nature Reviews Earth & Environment',
    'natrevelectreng': 'Nature Reviews Electrical Engineering', 'nrendo': 'Nature Reviews Endocrinology',
    'nrgastro': 'Nature Reviews Gastroenterology & Hepatology', 'nrg': 'Nature Reviews Genetics',
    'nri': 'Nature Reviews Immunology', 'natrevmats': 'Nature Reviews Materials',
    'nrmp': 'Nature Reviews Methods Primers', 'nrmicro': 'Nature Reviews Microbiology',
    'nrm': 'Nature Reviews Molecular Cell Biology', 'nrneph': 'Nature Reviews Nephrology',
    'nrneurol': 'Nature Reviews Neurology', 'nrn': 'Nature Reviews Neuroscience',
    'natrevphys': 'Nature Reviews Physics', 'nrpsychol': 'Nature Reviews Psychology',
    'nrrheum': 'Nature Reviews Rheumatology', 'nrurol': 'Nature Reviews Urology',
}
# Keep saved presets and cached ISSNs compatible with earlier bootstrap slugs.
# Canonical slugs are verified against https://www.nature.com/siteindex.
NATURE_ALIASES = {
    'natenergy': 'nenergy', 'natmicrobiol': 'nmicrobiol', 'nn': 'neuro',
    'natrevmethodsprimers': 'nrmp', 'natrevpsychol': 'nrpsychol',
}
NATURE = {slug: CORE['nature'] if slug == 'nature' else Journal(slug, title, rss=f'https://www.nature.com/{slug}.rss') for slug, title in _NATURE.items()}


def repair_generated_feed(journal):
    # Nature Digest is in the site index, but its generated /ndigest.rss
    # endpoint returns 404. Retain the title and ISSNs for Crossref, including
    # entries loaded from the persisted catalogue. Do not alter custom feeds.
    if (journal.id == 'ndigest' and journal.title == 'Nature Digest'
            and journal.rss == 'https://www.nature.com/ndigest.rss'):
        return replace(journal, rss=None)
    return journal


class NatureIndex(HTMLParser):
    def __init__(self):
        super().__init__()
        self.link = None
        self.text = []
        self.journals = {}

    def handle_starttag(self, tag, attrs):
        if tag == 'a':
            self.link = dict(attrs).get('href')
            self.text = []

    def handle_data(self, data):
        if self.link:
            self.text.append(data)

    def handle_endtag(self, tag):
        if tag != 'a' or not self.link:
            return
        title = ' '.join(''.join(self.text).split())
        url = urlsplit(urljoin('https://www.nature.com/', self.link))
        slug = url.path.strip('/')
        not_journals = {'Nature Portfolio', 'Nature Research', 'Nature Index', 'Nature Careers', 'Nature Masterclasses', 'Nature Events', 'Nature Africa', 'Nature India', 'Nature Middle East', 'Nature Outlook', 'Nature Podcast', 'Nature Video'}
        if title not in not_journals and url.hostname == 'www.nature.com' and re.fullmatch(r'[a-z][a-z0-9-]*', slug) and (title == 'Nature' or title.startswith('Nature ')):
            self.journals[slug] = repair_generated_feed(Journal(slug, title, rss=f'https://www.nature.com/{slug}.rss'))
        self.link = None


def discover_nature(html: str) -> dict[str, Journal]:
    parser = NatureIndex()
    parser.feed(html)
    if len(parser.journals) < 20:
        raise ValueError('Nature site index did not contain a usable journal catalogue')
    return parser.journals


def selected_journals(config, discovered=None) -> list[Journal]:
    nature = dict(NATURE)
    for journal in (discovered or {}).values():
        journal = repair_generated_feed(journal)
        canonical = NATURE_ALIASES.get(journal.id, journal.id)
        if canonical != journal.id:
            # A pre-fix catalogue can outlive a deployment. Repair only the
            # exact legacy feed we generated, not a separately supplied URL.
            old_rss = f'https://www.nature.com/{journal.id}.rss'
            journal = replace(journal, id=canonical,
                              rss=NATURE[canonical].rss if journal.rss == old_rss else journal.rss)
        # Live/cached official discovery wins if a publisher changes its slug.
        # Match complete titles only; do not merge similarly named journals.
        title = ' '.join(journal.title.split()).casefold()
        nature = {key: value for key, value in nature.items()
                  if key == journal.id or ' '.join(value.title.split()).casefold() != title}
        nature[journal.id] = journal
    available = {**nature, **CORE}
    def selected_id(key):
        canonical = NATURE_ALIASES.get(key, key)
        if canonical not in available and canonical in NATURE:
            title = ' '.join(NATURE[canonical].title.split()).casefold()
            canonical = next((j.id for j in nature.values()
                              if ' '.join(j.title.split()).casefold() == title), canonical)
        return canonical

    selected = []
    for key in config.get('presets', []):
        if key == 'nature_family':
            selected.extend(available[key] for key in nature)
        else:
            canonical = selected_id(key)
            if canonical not in available:
                raise ValueError(f'Unknown journal preset: {key}')
            selected.append(available[canonical])
    for item in config.get('custom', []):
        selected.append(Journal(selected_id(item['id']), item['title'], tuple(item.get('issns', [])), item.get('rss')))
    return list({j.id: j for j in selected}.values())
