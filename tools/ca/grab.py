"""Daily current affairs for Talati from many sources (sources.json).

1. Fetch every feed, keep items from the last --hours.
2. Group the same story told by several sources (title word overlap).
3. Drop noise (crime, accidents, gossip, party politics) and rank the rest for
   exam value: keywords, category, how many sources carry it, official source,
   and whether exam-prep sites ("signal" feeds) cover the same topic.
4. One AI call picks the best --pick stories; then, per story, the AI writes a
   short bilingual note and one MCQ from the article text of up to 3 sources
   (facts only, in our own words; never copying the signal sites).
5. Validate and write <out>/ca/<date>.json and index.json (same format the app
   reads; generate.py's PIB-only output stays compatible).

Usage:
  python tool/ca/grab.py --out D:/DHOLA/feeds/talati            # full run (needs AI keys)
  python tool/ca/grab.py --out ... --dry-run                     # fetch + rank only, prints the shortlist
"""
import argparse
import datetime as dt
import email.utils
import html
import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from generate import CATEGORIES, ask, get, load_keys, valid  # noqa: E402

HERE = Path(__file__).parent

NOISE = re.compile(
    r'\b(murder|killed|dies|death|dead|rape|assault|arrest|police|accident|crash|fire breaks|suicide|'
    r'stabbed|robbery|fraud case|court grants bail|bollywood|actor|actress|celebrity|wedding|box office|'
    r'trailer|horoscope|astrology|rally|slams|attacks|hits out|jibe|campaign trail|ticket to|poll body|'
    r'counting underway|bypoll|alliance talks|weather today|rain alert|rain forecast|unseasonal rain|stock tips|'
    r'sensex today|nifty today|nifty 50|live updates|garba|navratri|courtesy visit|price in india|smartphone|'
    r'power bank|mutual fund|index fund|inshorts|seeks bail|news today|marathon|'
    r'ગરબા|વરસાદ|આગાહી|અકસ્માત|હત્યા|ધરપકડ|આજની મોટી ખબરો)\b|launched in india at',
    re.I)
VALUE = re.compile(
    r'\b(launch|launched|scheme|yojana|mission|appointed|appoints|new (chief|chairman|director|governor|ceo)|'
    r'award|awarded|prize|honour|index|ranking|ranked|report|survey|gdp|inflation|repo|rbi|budget|policy|'
    r'mou|agreement|summit|exercise|isro|satellite|drdo|missile|first (ever|time)|world record|gold|medal|'
    r'championship|cup|olympic|day\b|anniversary|unesco|gi tag|tiger reserve|ramsar|national park|census|'
    r'bill passed|act|amendment|supreme court rules|constitution|panchayat|gujarat|ahmedabad|gandhinagar|'
    r'surat|vadodara|rajkot|kutch|narmada|sardar sarovar)\b',
    re.I)
STOP = set('a an the of in on at to for and or with by from is are was were be as its it this that new india indian '
           'says said will over after into about amid'.split())


def words(t):
    return {w for w in re.findall(r'[a-z0-9\u0900-\u0AFF]+', t.lower()) if w not in STOP and len(w) > 2}


def parse_feed(src, hours):
    try:
        xml = get(src['url'])
    except Exception as e:  # noqa: BLE001 - one dead feed must not stop the run
        print(f'  feed {src["id"]} failed: {e}', file=sys.stderr)
        return []
    out = []
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=hours)
    for item in re.findall(r'<item[ >].*?</item>', xml, re.S):
        def tag(n):
            m = re.search(rf'<{n}[^>]*>(.*?)</{n}>', item, re.S)
            if not m:
                return ''
            v = re.sub(r'<!\[CDATA\[(.*?)\]\]>', r'\1', m.group(1), flags=re.S)
            return html.unescape(re.sub(r'<[^>]+>', ' ', html.unescape(v))).strip()
        title, link = tag('title'), tag('link')
        if not title or not link:
            continue
        when = None
        if pd := tag('pubDate'):
            try:
                when = email.utils.parsedate_to_datetime(pd)
                if when.tzinfo is None:
                    when = when.replace(tzinfo=dt.timezone.utc)
            except (TypeError, ValueError):
                pass
        if when and when < cutoff:
            continue
        # Google News titles end with " - Publisher".
        if 'news.google.com' in src['url']:
            title = re.sub(r'\s+-\s+[^-]+$', '', title)
        out.append({'src': src, 'title': re.sub(r'\s+', ' ', title), 'link': link,
                    'desc': re.sub(r'\s+', ' ', tag('description'))[:400], 'when': when})
    return out


def cluster(items):
    """Greedy grouping of the same story across sources."""
    groups = []
    for it in items:
        w = words(it['title'])
        best, score = None, 0.0
        for g in groups:
            j = len(w & g['w']) / max(1, len(w | g['w']))
            if j > score:
                best, score = g, j
        if best and score >= 0.45:
            best['items'].append(it)
            best['w'] |= w
        else:
            groups.append({'items': [it], 'w': set(w)})
    return groups


def rank(groups):
    signals = [words(i['title']) for g in groups for i in g['items'] if i['src']['kind'] == 'signal']
    ranked = []
    for g in groups:
        real = [i for i in g['items'] if i['src']['kind'] != 'signal']
        if not real:
            continue  # signal sites only steer, they are never the source of an item
        text = ' '.join(i['title'] + ' ' + i['desc'] for i in real)
        if NOISE.search(' '.join(i['title'] for i in real)):
            continue
        # Distinct value words, capped: one noisy word repeated in many headlines is not "value".
        s = 2.0 * min(4, len({m.group(0).lower() for m in VALUE.finditer(text)}))
        s += 1.5 * len({i['src']['id'] for i in real})
        s += 3 if any(i['src']['kind'] == 'official' for i in real) else 0
        s += 2 if any(i['src'].get('hint') == 'gujarat' for i in real) else 0
        overlap = max((len(g['w'] & sw) / max(1, len(sw)) for sw in signals), default=0)
        s += 6 * overlap  # exam-prep sites cover it: likely asked
        if s >= 3:
            ranked.append((s, g))
    ranked.sort(key=lambda x: -x[0])
    return ranked


def article_text(url):
    try:
        page = get(url, timeout=20)
    except Exception:  # noqa: BLE001
        return ''
    page = re.sub(r'<script.*?</script>|<style.*?</style>|<nav.*?</nav>|<footer.*?</footer>', '', page,
                  flags=re.S | re.I)
    paras = [html.unescape(re.sub(r'<[^>]+>', '', p)).strip() for p in re.findall(r'<p[^>]*>(.*?)</p>', page, re.S)]
    paras = [re.sub(r'\s+', ' ', p) for p in paras if len(p) > 60]
    return '\n'.join(paras)[:5000]


PICK = """You choose current affairs for candidates of the Gujarat Talati (village panchayat
secretary) and other Gujarat state exams. From the numbered news stories below, pick
the {n} most useful for such an exam: government schemes and policies, appointments,
awards, indices and reports, important days, science/space/defence, economy figures,
environment, sports results, international agreements, and anything about Gujarat.
Skip crime, accidents, opinion, party politics and routine news. Prefer a balanced mix.
Several numbers may be the same story from different outlets: pick it only once.

Reply with ONLY a JSON object: {{"pick": [{{"n": <story number>, "category": one of {cats}}}]}}

STORIES:
{stories}
"""

WRITE = """Write one current-affairs item for Gujarat Talati exam candidates, using ONLY the
facts in the source texts below (several reports of the same story). Use your own
words; never copy sentences. If the sources disagree on a fact, leave that fact out.

Reply with ONLY a JSON object:
{{
 "category": "{cat}",
 "title_en": "headline, max 12 words",
 "title_gu": "same headline in natural Gujarati",
 "summary_en": "2-3 sentences with the exam-relevant facts (names, numbers, places, dates)",
 "summary_gu": "the same in simple, natural Gujarati (Gujarati script, not Hindi)",
 "mcq": {{
   "q_en": "one exam-style question answerable from these facts",
   "q_gu": "the same question in Gujarati",
   "o_en": ["4 options"], "o_gu": ["the same 4 options in Gujarati, same order"],
   "a": index 0-3 of the correct option,
   "e_en": "one-line explanation", "e_gu": "the same in Gujarati"
 }}
}}

SOURCES:
{sources}
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--out', required=True)
    ap.add_argument('--hours', type=int, default=30, help='how far back to read feeds')
    ap.add_argument('--pick', type=int, default=12, help='items per day')
    ap.add_argument('--shortlist', type=int, default=45, help='stories shown to the picker')
    ap.add_argument('--dry-run', action='store_true')
    args = ap.parse_args()
    load_keys()
    sources = json.loads((HERE / 'sources.json').read_text(encoding='utf-8'))['sources']
    with ThreadPoolExecutor(8) as pool:
        items = [i for batch in pool.map(lambda s: parse_feed(s, args.hours), sources) for i in batch]
    print(f'{len(items)} items from {len(sources)} feeds')

    out = Path(args.out) / 'ca'
    seen_path = out / 'seen.json'
    seen = set(json.loads(seen_path.read_text(encoding='utf-8'))) if seen_path.exists() else set()
    items = [i for i in items if i['link'] not in seen]
    ranked = rank(cluster(items))[:args.shortlist]
    print(f'{len(ranked)} stories shortlisted')
    if args.dry_run:
        for s, g in ranked:
            srcs = ', '.join(sorted({i['src']['id'] for i in g['items']}))
            print(f'  {s:5.1f}  {g["items"][0]["title"][:90]}  [{srcs}]')
        return

    stories = '\n'.join(f'{n}. {g["items"][0]["title"]} | {g["items"][0]["desc"][:160]}'
                        for n, (_, g) in enumerate(ranked))
    _, picked = ask(PICK.format(n=args.pick, cats=CATEGORIES, stories=stories))
    choices = [(p['n'], p.get('category', 'national')) for p in (picked or {}).get('pick', [])
               if isinstance(p.get('n'), int) and 0 <= p['n'] < len(ranked)][:args.pick]
    print(f'{len(choices)} picked')

    out.mkdir(parents=True, exist_ok=True)
    today = dt.date.today().isoformat()
    day_path = out / f'{today}.json'
    day = json.loads(day_path.read_text(encoding='utf-8')) if day_path.exists() else {'date': today, 'items': []}
    have = {i['id'] for i in day['items']}
    for n, cat in choices:
        g = ranked[n][1]
        real = sorted((i for i in g['items'] if i['src']['kind'] != 'signal'),
                      key=lambda i: i['src']['kind'] != 'official')[:3]
        texts = []
        for i in real:
            body = article_text(i['link']) if 'news.google.com' not in i['link'] else ''
            texts.append(f'[{i["src"]["name"]}] {i["title"]}\n{body or i["desc"]}')
        provider, item = ask(WRITE.format(cat=cat if cat in CATEGORIES else 'national', sources='\n\n'.join(texts)))
        for i in g['items']:
            seen.add(i['link'])
        if not item:
            continue
        item['useful'] = True
        problems = valid(item)
        if problems:
            print(f'  rejected: {problems}', file=sys.stderr)
            continue
        item.pop('useful', None)
        iid = 'ca-' + re.sub(r'[^a-z0-9]+', '-', item['title_en'].lower()).strip('-')[:60]
        if iid in have:
            continue
        have.add(iid)
        item.update({'id': iid, 'source': real[0]['link'],
                     'sources': [{'name': i['src']['name'], 'url': i['link']} for i in real], 'by': provider})
        day['items'].append(item)
        print(f'  + {item["category"]}: {item["title_en"]}')

    if day['items']:
        day_path.write_text(json.dumps(day, ensure_ascii=False, indent=1) + '\n', encoding='utf-8')
    seen_path.write_text(json.dumps(sorted(seen)[-5000:]) + '\n', encoding='utf-8')
    days = sorted((p.stem for p in out.glob('????-??-??.json')), reverse=True)[:60]
    index = [{'date': d, 'n': len(json.loads((out / f'{d}.json').read_text(encoding='utf-8'))['items'])} for d in days]
    (out / 'index.json').write_text(json.dumps({
        'updated': dt.datetime.now().isoformat(timespec='minutes'),
        'source': 'Official releases (PIB) and news reports; summaries written for exam revision',
        'days': index}, ensure_ascii=False, indent=1) + '\n', encoding='utf-8')
    print(f'{len(day["items"])} items for {today}')


if __name__ == '__main__':
    main()
