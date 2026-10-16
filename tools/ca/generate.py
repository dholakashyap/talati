"""Daily current affairs for Talati, from official Government of India press
releases (PIB), turned into short bilingual notes and exam-style questions by a
free AI model. Every item keeps the link to its source release.

Pipeline: PIB RSS -> release text -> AI (Gemini, else Groq, else OpenRouter)
-> validation -> <out>/ca/<yyyy-mm-dd>.json + <out>/ca/index.json

Keys come from the environment or D:/DHOLA/secrets/ai.env (never committed):
  GEMINI_API_KEY, GROQ_API_KEY, OPENROUTER_API_KEY
  optional: GEMINI_MODEL, GROQ_MODEL, OPENROUTER_MODEL

Usage:
  python tool/ca/generate.py --out D:/DHOLA/feeds/talati          # today's new releases
  python tool/ca/generate.py --out ... --max 25 --dry-run         # fetch only, no AI, no writes
"""
import argparse
import datetime as dt
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

RSS = [
    # PIB national releases (the feed serves Hindi and English titles).
    'https://pib.gov.in/RssMain.aspx?ModId=6&Lang=1&Regid=3',
]
UA = 'Mozilla/5.0 (TalatiExamPrep content builder; contact dholainfotech)'
SECRETS = Path('D:/DHOLA/secrets/ai.env')
CATEGORIES = ['national', 'gujarat', 'schemes', 'economy', 'science', 'defence', 'environment',
              'sports', 'awards', 'appointments', 'international', 'days', 'culture']


def load_keys():
    if SECRETS.exists():
        for line in SECRETS.read_text(encoding='utf-8').splitlines():
            if '=' in line and not line.strip().startswith('#'):
                k, v = line.split('=', 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"'))


def get(url, timeout=30):
    req = urllib.request.Request(url, headers={'User-Agent': UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode('utf-8', errors='replace')


def releases():
    out = []
    for feed in RSS:
        xml = get(feed)
        for item in re.findall(r'<item>(.*?)</item>', xml, re.S):
            title = re.search(r'<title>(.*?)</title>', item, re.S)
            link = re.search(r'<link>(.*?)</link>', item, re.S)
            if not (title and link):
                continue
            url = html.unescape(link.group(1).strip())
            prid = re.search(r'PRID=(\d+)', url)
            if prid:
                out.append({'id': prid.group(1), 'title': html.unescape(title.group(1).strip()),
                            'url': f'https://pib.gov.in/PressReleasePage.aspx?PRID={prid.group(1)}'})
    return out


def release_text(prid):
    page = get(f'https://pib.gov.in/PressReleaseIframePage.aspx?PRID={prid}')
    page = re.sub(r'<script.*?</script>|<style.*?</style>', '', page, flags=re.S | re.I)
    text = html.unescape(re.sub(r'<[^>]+>', '\n', page))
    lines = [ln.strip() for ln in text.split('\n') if len(ln.strip()) > 40]
    return '\n'.join(lines)[:6000]


PROMPT = """You prepare current affairs for candidates of the Gujarat Talati (village
panchayat secretary) exam. Below is one official Government of India press release.

Decide if it is useful for a competitive exam (new schemes, appointments, awards,
important days, sports results, science and space, defence exercises, economy figures,
environment, international agreements, anything about Gujarat). Routine greetings,
meetings without outcomes, and campaign/cleanliness drives are NOT useful.

Reply with ONLY a JSON object:
{
 "useful": true|false,
 "category": one of %s,
 "title_en": "short headline, max 12 words",
 "title_gu": "same headline in natural Gujarati",
 "summary_en": "2-3 sentences with the exam-relevant facts (names, numbers, places, dates)",
 "summary_gu": "the same summary in natural, simple Gujarati",
 "mcq": {
   "q_en": "one exam-style question answerable ONLY from this release",
   "q_gu": "the same question in Gujarati",
   "o_en": ["4 options"], "o_gu": ["the same 4 options in Gujarati, same order"],
   "a": index 0-3 of the correct option,
   "e_en": "one-line explanation quoting the fact", "e_gu": "the same in Gujarati"
 }
}
Rules: use only facts stated in the release; never invent numbers or names; keep proper
nouns correct; Gujarati must be grammatical Gujarati script (not Hindi). If useful is
false, the other fields may be empty.

RELEASE TITLE: %s
RELEASE TEXT:
%s
"""


def _post(url, body, headers):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={'Content-Type': 'application/json', **headers})
    with urllib.request.urlopen(req, timeout=90) as r:
        return json.loads(r.read().decode())


def ask_gemini(prompt):
    key = os.environ.get('GEMINI_API_KEY')
    if not key:
        return None
    model = os.environ.get('GEMINI_MODEL', 'gemini-2.5-flash')
    r = _post(f'https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}',
              {'contents': [{'parts': [{'text': prompt}]}],
               'generationConfig': {'responseMimeType': 'application/json', 'temperature': 0.2}}, {})
    return r['candidates'][0]['content']['parts'][0]['text']


def _openai_style(url, key, model, prompt):
    r = _post(url, {'model': model, 'temperature': 0.2, 'response_format': {'type': 'json_object'},
                    'messages': [{'role': 'user', 'content': prompt}]},
              {'Authorization': f'Bearer {key}'})
    return r['choices'][0]['message']['content']


def ask_groq(prompt):
    key = os.environ.get('GROQ_API_KEY')
    if not key:
        return None
    return _openai_style('https://api.groq.com/openai/v1/chat/completions', key,
                         os.environ.get('GROQ_MODEL', 'llama-3.3-70b-versatile'), prompt)


def ask_openrouter(prompt):
    key = os.environ.get('OPENROUTER_API_KEY')
    if not key:
        return None
    return _openai_style('https://openrouter.ai/api/v1/chat/completions', key,
                         os.environ.get('OPENROUTER_MODEL', 'meta-llama/llama-3.3-70b-instruct:free'), prompt)


def ask(prompt):
    """First provider that answers with valid JSON wins."""
    for name, fn in [('gemini', ask_gemini), ('groq', ask_groq), ('openrouter', ask_openrouter)]:
        try:
            text = fn(prompt)
            if text is None:
                continue
            text = re.sub(r'^```(json)?|```$', '', text.strip(), flags=re.M).strip()
            return name, json.loads(text)
        except (urllib.error.URLError, KeyError, IndexError, ValueError, TimeoutError) as e:
            print(f'  {name} failed: {e}', file=sys.stderr)
            time.sleep(2)
    return None, None


def valid(item):
    """Problems with an AI answer; empty list = fine."""
    p = []
    for k in ('title_en', 'title_gu', 'summary_en', 'summary_gu'):
        if not str(item.get(k, '')).strip():
            p.append(f'missing {k}')
    if item.get('category') not in CATEGORIES:
        p.append('bad category')
    if not re.search('[\u0A80-\u0AFF]', item.get('summary_gu', '')):
        p.append('summary_gu is not Gujarati script')
    m = item.get('mcq') or {}
    for k in ('o_en', 'o_gu'):
        if not (isinstance(m.get(k), list) and len(m[k]) == 4 and all(str(o).strip() for o in m[k])):
            p.append(f'mcq {k} needs 4 options')
    if m.get('a') not in (0, 1, 2, 3):
        p.append('mcq answer index')
    if len(set(map(str, m.get('o_en', [])))) != 4:
        p.append('duplicate options')
    return p


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--out', required=True, help='content repo folder; files go to <out>/ca/')
    ap.add_argument('--max', type=int, default=20, help='releases to look at per run')
    ap.add_argument('--dry-run', action='store_true')
    args = ap.parse_args()
    load_keys()
    out = Path(args.out) / 'ca'
    out.mkdir(parents=True, exist_ok=True)
    seen_path = out / 'seen.json'
    seen = set(json.loads(seen_path.read_text(encoding='utf-8'))) if seen_path.exists() else set()

    today = dt.date.today().isoformat()
    day_path = out / f'{today}.json'
    day = json.loads(day_path.read_text(encoding='utf-8')) if day_path.exists() else {'date': today, 'items': []}

    fresh = [r for r in releases() if r['id'] not in seen][:args.max]
    print(f'{len(fresh)} new releases')
    for r in fresh:
        try:
            text = release_text(r['id'])
        except Exception as e:  # noqa: BLE001 - one bad page must not stop the run
            print(f'  skip {r["id"]}: {e}', file=sys.stderr)
            continue
        if args.dry_run:
            print(f'  {r["id"]} {r["title"][:70]} ({len(text)} chars)')
            continue
        provider, item = ask(PROMPT % (CATEGORIES, r['title'], text))
        seen.add(r['id'])
        if not item or not item.get('useful'):
            continue
        problems = valid(item)
        if problems:
            print(f'  rejected {r["id"]}: {problems}', file=sys.stderr)
            continue
        item.pop('useful', None)
        item.update({'id': f'ca-{r["id"]}', 'source': r['url'], 'by': provider})
        day['items'].append(item)
        print(f'  + {item["category"]}: {item["title_en"]}')

    if args.dry_run:
        return
    if day['items']:
        day_path.write_text(json.dumps(day, ensure_ascii=False, indent=1) + '\n', encoding='utf-8')
    seen_path.write_text(json.dumps(sorted(seen)) + '\n', encoding='utf-8')
    # Index of the last 60 days that have items, newest first.
    days = sorted((p.stem for p in out.glob('????-??-??.json')), reverse=True)[:60]
    index = [{'date': d, 'n': len(json.loads((out / f'{d}.json').read_text(encoding='utf-8'))['items'])} for d in days]
    (out / 'index.json').write_text(json.dumps({'updated': dt.datetime.now().isoformat(timespec='minutes'),
                                                'source': 'Press Information Bureau, Government of India (pib.gov.in)',
                                                'days': index}, ensure_ascii=False, indent=1) + '\n', encoding='utf-8')
    print(f'{len(day["items"])} items for {today}')


if __name__ == '__main__':
    main()
