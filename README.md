# Talati Exam Prep: content feed

Content that the Talati app (`com.dhola.talati`) downloads, so it can be updated
without a new app release.

| Path | What | Written by |
|---|---|---|
| `ca/index.json` | Last 60 days that have current affairs | `tools/ca/grab.py` |
| `ca/<yyyy-mm-dd>.json` | That day's items: bilingual note, one MCQ, source links | `tools/ca/grab.py` |
| `ca/seen.json` | Links already processed (so nothing is written twice) | `tools/ca/grab.py` |

The app reads `https://raw.githubusercontent.com/dholakashyap/talati/main/ca/`.

## How current affairs are made

`.github/workflows/current-affairs.yml` runs twice a day on GitHub (07:00 and 18:00 IST):

1. Reads the feeds in `tools/ca/sources.json`: PIB (official), The Hindu, Indian Express,
   Business Standard, Sportstar and Google News searches for Gujarat government news.
   Exam-prep sites (AffairsCloud, Drishti IAS) are read only to judge which topics exams
   care about; nothing is taken from them.
2. Groups the same story across outlets, drops crime, accidents, gossip and party politics,
   and ranks the rest.
3. An AI model (Gemini; Groq or OpenRouter if Gemini fails) picks the best 12 and writes each
   one as a short English + Gujarati note and one question, using only facts from the
   reports, in its own words. Every item keeps links to its sources.

Keys are repository secrets (`GEMINI_API_KEY`, `GROQ_API_KEY`, `OPENROUTER_API_KEY`), never
files in the repo. `tools/ca` is a copy of `apps/talati/tool/ca` in the app repo; change it
there and copy it here.

Run by hand: `python tools/ca/grab.py --out . --dry-run` shows today's shortlist without AI.
