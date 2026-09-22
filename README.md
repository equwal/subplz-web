# SubPlz Web

Line an audiobook up with its ebook, sentence by sentence.

![subread.space: drop an audiobook and its ebook, get subtitles, videos and a read-along book](docs/screenshots/home.png)

Drop in an audiobook and the ebook it was read from. You get back subtitles
timed to the narration, with the wording taken from your own book rather than
from a machine's guess at what it heard. Two things to do with that:

- **HoshiReader whispersync** — the `.srt` is the timing file.
- **Subtitled video** — a YouTube-ready MP4 with a selectable caption track.

Free in the browser, without limit; see [Pricing](#pricing).

---

## Quick start

```powershell
.\run.ps1
```

Opens <http://127.0.0.1:8420>. First run creates `.venv` and installs
everything, including the alignment backend — several minutes, because it
pulls torch.

Already set up:

```powershell
.venv\Scripts\python.exe -m uvicorn backend.main:app --port 8420
```

Requires **ffmpeg on PATH** and **Python 3.11** — subplz pins `>=3.10,<3.12`,
so 3.12+ will not work.

---

## About "no Whisper"

This app never runs `subplz gen`, the mode that transcribes a book from scratch.
Only `subplz sync` is reachable, and `gen` is not exposed anywhere in the API.

That said, `subplz sync` is itself built on Whisper, and there is no way around
that. It works like this:

1. A **tiny** Whisper model makes a rough transcript of the audio.
2. That transcript is aligned to *your* ebook text with Needleman–Wunsch.
3. The subtitle text that gets written is **your book's text**, not Whisper's.

So Whisper is used as a timing device, not as a source of words. Nothing it
mis-hears reaches the `.srt`. If "no Whisper" meant "no model downloads and no
transcription step at all", subplz cannot do that — you would need a different
aligner (aeneas, Montreal Forced Aligner, WhisperX) and a different tool.

---

## Input formats

**Audio** — `m4b`, `mp3`, `m4a`, `opus`, `flac`, `wav`, `mkv` and friends.
A single file, or **a folder of per-chapter files**: drop all 44 mp3s and they
are merged into one chaptered file, in natural order (`9.mp3` before `10.mp3`).
Files can arrive one drop at a time; the upload starts once both halves are in.

**Book** — `epub`, `txt`, `srt`, `vtt`, `ass`, `fb2`, `fb2.zip`, and the
Kindle formats `mobi`, `azw3`, `azw` and `prc`. In the browser every one of
them is read in the tab (`frontend/engine/book.js`; the Kindle formats through
[foliate-js](https://github.com/johnfactotum/foliate-js), fetched by
`tools/fetch_vendor.py`). A server job converts the same formats on upload.

Conversion on the server delegates rather than parsing ebook formats by hand
(`backend/convert.py`):

| From | How |
|------|-----|
| `azw3` / KF8 | the `mobi` package unpacks it straight to epub |
| `mobi` (older) | `mobi` unpacks to HTML, ebooklib rebuilds the epub |
| `fb2`, `fb2.zip` | it is XML; lxml reads it, ebooklib writes the epub |

`mobi` is the only added dependency; lxml, BeautifulSoup and ebooklib already
ship with subplz.

**Conversion must preserve chapters**, and that is not a stylistic preference.
See [Will this text even match?](#will-this-text-even-match).

---

## Will this text even match?

subplz fails late and unhelpfully when the text does not match the audio: it
transcribes the whole book, then reports "the generated transcript and the
provided text file are too different" and writes a `.subfail`. On CPU that is a
wasted hour. This app answers the question on upload, in seconds — and answers it
better than subplz can.

### Why a fixed threshold cannot work

subplz compares chapters with `rapidfuzz.fuzz.ratio` against a constant
`SCORE_THRESHOLD = 40`. What two *unrelated* chapters score depends entirely on
how many characters the script has:

| noise floor, unrelated chapters of one book | `fuzz.ratio` |
|---|---|
| Japanese | 23 – 26 |
| Russian | 39 |
| Spanish | 44 |
| English | 45 – 47 |

For English and Spanish the floor is **above 40**, so unrelated chapters clear
the gate and it discriminates nothing. The constant suits Japanese, which is what
it was tuned on: thousands of distinct characters make coincidental similarity
rare, while twenty-six letters make it inevitable. Cleaning does not help — a
generic punctuation strip moved English only 45.3 → 42.4, because the cause is
alphabet size, not typography.

### What this app does instead — `backend/chapters.py`

**Character 4-gram overlap, not edit distance.** Measured against the same audio:

| metric | ru floor | en floor | ja floor | true match | runner-up | separation |
|---|---|---|---|---|---|---|
| `fuzz.ratio` | 39.2 | 45.1 | 26.1 | 86.3 | 40.8 | 2.1× |
| word Jaccard | 10.4 | 17.7 | 1.9 | 63.2 | 14.8 | 4.3× |
| **4-gram Jaccard** | **4.8** | **10.6** | **2.5** | 53.3 | 5.6 | **9.5×** |

n-grams rather than words because many languages do not separate words with
spaces — a word-level metric collapses on Japanese, where a "word" is a whole run
of characters. Set intersection is also cheaper than edit distance.

**A threshold calibrated per book, not per release.** Before judging anything,
the app samples ~150 unrelated chapter pairs *from the book in hand* to learn what
it scores by chance, then requires a match to clear that floor. One rule that
behaves correctly whether the floor is 2.5 or 45, with no constant to re-tune.

**A runner-up test, which is what actually catches a wrong book.** A different
book in the same language scores *high* — it shares a vocabulary. What it cannot
do is make one chapter stand out. Measured, same audio:

| book | best | runner-up | confidence | accept_at | verdict |
|---|---|---|---|---|---|
| the right one | 78 – 87 | ~30 | **2.7 – 2.9×** | 30.7 | accepted 4/4 |
| an unrelated Russian novel | 55 – 60 | 53 – 58 | **1.0×** | 92.1 | rejected 4/4 |

Note the second row clears subplz's threshold of 40 comfortably and is still
rejected here, on both tests independently: its own noise floor is 57.6, so
`accept_at` rises to 92, and nothing stands out from the crowd.

**Adaptive sampling.** Chapters are sampled from across the book, skipping
chapter 0 — publisher announcements and credits live there, so it is the least
representative chapter in the book. Sampling stops as soon as one chapter matches
confidently, so a good pair typically costs ~8s and a doubtful one ~20s.

A poor verdict warns and relabels the button "Start anyway"; it never blocks,
because the check only samples and it is your book. Disable it entirely with
`SUBPLZ_WEB_MATCH_CHECK=false`.

### Measured and rejected

- **Typographic normalisation** of quotes, dashes, soft hyphens, NBSP and
  footnote markers: **+0.0**. Built, measured, deleted.
- **Un-gluing sentence boundaries** (`роса…Мне` → `роса… Мне`): multi-sentence
  lines 13.6% → 12.4%, because pysbd does not treat `…` as a terminator for
  Russian. Not worth the epub rewrite.
- **`token_set_ratio` / `token_sort_ratio`**: *worse* than `fuzz.ratio` on long
  texts — nearly every common word appears in both chapters.

---

## Languages

97 languages. The catch upstream does not document: subplz splits sentences with
`pysbd`, which supports **23** languages and raises `ValueError` on anything
else — **Portuguese and Finnish included**. subplz can use `stanza` instead when
given `--nlp`, which covers 74 more.

This app resolves that per request. `backend/languages.json` records which
splitter each language needs, and the aligner adds `--nlp` automatically. You
never see the failure.

| Language | Splitter | Notes |
|---|---|---|
| Spanish, Russian, Japanese | pysbd | fast path |
| Portuguese, Finnish | stanza | one-off model download on first use |

subplz also defaults `--language` **and** `--lang` to Japanese. The aligner
always sets both explicitly, so a non-Japanese book cannot silently run as
Japanese.

Regenerate the registry after upgrading pysbd or stanza:

```powershell
.venv\Scripts\python.exe -m backend.gen_languages
```

---

## How it works

```
drop files ──► POST /api/uploads        stage, pair, convert, detect language
                     │
                     ▼                  (draft — correct the language here)
               POST /api/jobs/{id}/start    entitlement check, enqueue
                     │
                     ▼
               queue ──► runner ──► aligner ──► .srt ──► .mp4
                                        │
                                        ▼
                                   storage + metadata.json
```

Upload and start are separate calls on purpose: a wrong language guess costs a
click instead of a re-upload and a wasted multi-hour run.

### Audio is prepared twice, deliberately

The aligner is fed a **16 kHz mono** copy; the video keeps the original quality.
That is not tidiness — handing subplz 44.1 kHz stereo crashed ctranslate2 here
(integer divide by zero, part-way through a chapter), reproducibly. Doing the
conversion ourselves also repairs damaged input: the error-tolerant ffmpeg flags
drop corrupt frames instead of letting a single bad chapter abort the whole run.

### Files

| Path | Role |
|---|---|
| `backend/api.py` | HTTP routes |
| `backend/aligner.py` | the alignment backend, behind an interface |
| `backend/runner.py` | stages inputs, drives the aligner, collects artifacts |
| `backend/convert.py` | fb2/mobi/azw3 → a chaptered epub |
| `backend/matching.py` | the preflight match score |
| `backend/render.py` | the YouTube MP4 |
| `backend/detect.py` | pairs the dropped files, detects the language |
| `backend/languages.py` | language registry and splitter routing |
| `backend/queue.py` | in-process or Redis dispatch |
| `backend/storage.py` | local disk or S3 |
| `backend/billing.py` | who may do what: free in the browser, a credit for a server job |
| `backend/payments.py` | Stripe: checkout, idempotent fulfilment, webhooks |
| `backend/accounts.py` | one email, one account; folding anonymous work in |
| `backend/auth.py` | emailed one-time sign-in links |
| `backend/pricing.py` | the plan catalogue, with the market it was set against |
| `frontend/` | vanilla HTML/CSS/JS, no build step |
| `tools/client.py` | CLI client, and a worked example of the API |
| `worker.py` | standalone worker for the Redis backend |

### Outputs

| Artifact | What |
|---|---|
| `<name>.<lang>.srt` | the subtitles |
| `<name>.<lang>.mp4` | cover + audio + soft caption track (`mov_text`) |
| `metadata.json` | language, model, splitter, cue count, timing span |
| `subplz.log` | the full run log — the only way to debug a bad alignment |

A job that runs in the browser tab can also make `<name>.read-along.epub`: the
epub with the narration inside it, as EPUB 3 Media Overlays
(`frontend/engine/epub.js`). Thorium, Storyteller and other EPUB 3 readers play
it and highlight each line. The cues do not say where in the pages their words
are, so the engine finds each cue's words again in the text of the pages, in
order, and puts a `<span id>` around them. An EPUB 2 book becomes EPUB 3 (a
navigation document is made from the NCX). The audio must be MP3 or AAC in
m4a/m4b, which are the types an EPUB 3 reader must play. `tests/engine/epub.test.mjs`
follows each overlay as a reader does, and gives the result to the W3C
`epubcheck` when `EPUBCHECK` points to its jar.

The server keeps the files of each job together in `data/work/<job id>/`: the
audio and the book in `input/`, the subtitles in `out/`, and the run log. Nothing
deletes them: the operator deletes them by hand. A **failed** job can thus be
retried without a new upload.

---

## Swapping the alignment backend

subplz is one implementation of `aligner.Aligner`, not a hard dependency.
Everything subplz-specific — its argument names, its Japanese defaults, the
shape of its progress output, where it writes the result — lives in
`SubPlzAligner`. To replace it:

1. subclass `Aligner` (`build_command`, `progress_reader`, `locate_output`)
2. register it in `ALIGNERS`
3. set `SUBPLZ_WEB_ALIGNER` to its name

The API, queue, storage and job runner do not change.

---

## Video

A still cover image at 1 fps, the audio, and the subtitles as a **selectable
track** rather than burned in — so the file stays small, the encode stays fast,
and the viewer can turn captions off. The cover is the largest image in the
epub, or a plain dark card when there is not one.

The H.264 encoder is **probed, not assumed**: `ffmpeg -encoders` lists encoders
that were compiled in, including hardware ones on machines with no such
hardware, so the app encodes one test frame with each candidate and takes the
first that actually works. On this machine that is `h264_amf`; a build with
`libx264` will prefer that. Set `SUBPLZ_WEB_VIDEO_ENCODER` to force one, or
`SUBPLZ_WEB_RENDER_VIDEO=false` to skip video entirely.

---

## CLI

```powershell
.venv\Scripts\python.exe tools\client.py submit book.m4b book.epub --wait
.venv\Scripts\python.exe tools\client.py submit .\chapters\ book.epub --wait
.venv\Scripts\python.exe tools\client.py list
.venv\Scripts\python.exe tools\client.py download <job_id> --dir out\
```

Use this rather than `curl` for non-ASCII filenames: curl on a non-UTF-8 console
mangles multipart filenames, and this client does not.

---

## Pricing

All of the code is public, and anyone may host it. What this site sells is the
use of its operator's machines, and nothing else.

**Free and paid are split by where the work is done, not by what comes out.**

| Tier | Where it runs | You get | Costs |
|---|---|---|---|
| free | the visitor's browser (or the Android app) | each output: `.srt`, `.mkv`, `.mp4`, the read-along `.epub` | nothing, without limit |
| cloud | this server's hardware: Whisper tiny on a CPU, one book at a time | the same, from any device, with the tab closed: about 2 hours for a 10-hour book | one credit |

| Plan | Price | Per book |
|---|---|---|
| 10 books | $4.99 | $0.50 |
| 100 books | $39.99 | $0.40 |
| 500 books | $174.99 | $0.35 |

A book costs about $0.64 to convert on a rented GPU. Above about $5 a technical
buyer wraps a raw alignment API (ElevenLabs: $2.20 for a 10-hour book). Below
$3 the fixed card fee takes too much. There is no unlimited plan: use comes in
bursts, and one heavy user of such a plan costs more than the plan brings in.
An operator who wants a recurring plan adds one with `SUBPLZ_WEB_PLANS_JSON`.
Every number is an env var — see `backend/pricing.py`.

A job in the browser costs the server nothing, so it is never counted and never
refused. A server job takes its credit at the start. A job that fails or is
cancelled gets the credit back.

**Free credits.** They exist only while `SUBPLZ_WEB_CLOUD_ENABLED` is true.

- A visitor without a verified email gets one free credit
  (`SUBPLZ_WEB_FREE_CREDITS_ANONYMOUS`). A buyer whose email came only from
  Stripe is in this group.
- An account whose email is verified gets two in all
  (`SUBPLZ_WEB_FREE_CREDITS_VERIFIED`). The email is verified when its owner
  opens a sign-in link that the server sent to it. A free credit that a device
  used before counts toward the two, and a second device adds no free credits.
- A verified account also gets one free credit each day
  (`SUBPLZ_WEB_DAILY_FREE_CREDIT`). It does not carry over to the next day.
- A verified account gets one more free credit for every two books that the
  server converted for it (`SUBPLZ_WEB_BOOKS_PER_BONUS_CREDIT`). Books
  converted in the browser do not count, because the server cannot check them.

A server job spends the daily credit first, then a free credit, then a bought
one. The account counts the free credits apart from the bought ones
(`free_credits_used`), so a refund of bought credits never pays out a free one.

A switch above the drop zone says where the work is done. Each visit starts
with it on ("Convert in this browser (JavaScript)"), before a file is chosen.
Off: the files are uploaded, the job runs on the server, and the result waits
in the list of conversions, so the visitor can close the tab. The page asks
for the credit before the upload, not after it.

The server keeps each upload and each result, for debugging. Nothing deletes
them automatically: the operator deletes them by hand, and on request.
`/terms.html` says so to the visitor, and shows `SUBPLZ_WEB_CONTACT_EMAIL`.

`SUBPLZ_WEB_CLOUD_ENABLED` says that fast conversion is on offer. While it is
false the page shows no way to buy credits, whatever `SUBPLZ_WEB_BILLING_ENABLED`
says: credits that buy nothing must not be for sale.

**An account is an email.** It gets attached either by following an emailed
one-time link (no passwords anywhere) or by paying, since Stripe collects one.
Whatever the device converted while anonymous is folded into the account, and
the cookie is re-pointed at it - including when the payment lands by webhook
with no browser attached (`Account.merged_into`).

**Payments are Stripe Checkout**, with prices sent inline from
`backend/pricing.py`, so there is nothing to configure in the Stripe dashboard
beyond a webhook. Two rules: the browser returning from Stripe is never treated
as proof of payment (the session is re-read from Stripe), and fulfilment is
idempotent on the checkout session id, because Stripe reports one payment
several times.

### Turning payments on

```bash
# on the server, as root - each prompts for the value with input hidden
tools/set-secret.sh SUBPLZ_WEB_STRIPE_SECRET_KEY       # sk_live_... (or sk_test_...)
tools/set-secret.sh SUBPLZ_WEB_STRIPE_WEBHOOK_SECRET   # whsec_...
tools/set-secret.sh SUBPLZ_WEB_SMTP_PASSWORD           # for sign-in emails
```

In Stripe: Developers -> Webhooks -> add `https://<host>/api/billing/webhook`
with `checkout.session.completed`, `checkout.session.async_payment_succeeded`,
`customer.subscription.created`, `.updated` and `.deleted`. Then set
`SUBPLZ_WEB_BILLING_ENABLED=true`, `SUBPLZ_WEB_PUBLIC_BASE_URL`,
`SUBPLZ_WEB_COOKIE_SECURE=true` and the `SMTP_*` values in `.env` and restart.

Not handled: refunds and disputes (do them in the Stripe dashboard and adjust
`purchased_credits` by hand), and tax (Stripe Tax is one checkout parameter away
once you are registered somewhere).

---

## Releases

Every deploy is a tag (`vMAJOR.MINOR.PATCH`) with a GitHub release; `/healthz`
reports which one is running. Tag, push, then deploy that tag:

```bash
git tag -a v2.0.1 -m "what changed" && git push origin main --tags
gh release create v2.0.1 --notes "what changed"
```

---

## Scaling out

Everything environment-specific is an env var with a localhost default. See
`.env.example`.

| Concern | localhost | public |
|---|---|---|
| Queue | thread pool in the API process | Redis + `worker.py` |
| Storage | `./data/artifacts` | S3, presigned download URLs |
| Database | SQLite | Postgres |
| Identity | cookie | cookie + emailed sign-in links (`SMTP_*`) |
| Billing | off, nothing locked | `SUBPLZ_WEB_BILLING_ENABLED=true` + Stripe keys |

```bash
SUBPLZ_WEB_QUEUE_BACKEND=redis \
SUBPLZ_WEB_REDIS_URL=redis://redis:6379/0 \
SUBPLZ_WEB_DATABASE_URL=postgresql+psycopg://user:pass@host/subplz \
SUBPLZ_WEB_STORAGE_BACKEND=s3 SUBPLZ_WEB_S3_BUCKET=subplz-artifacts \
SUBPLZ_WEB_DEVICE=cuda \
python worker.py
```

With an external queue the API copies staged uploads into shared storage before
enqueuing, and the worker pulls them down, so the API and workers do not need a
shared filesystem.

### Before going public

- Set the Stripe keys, the webhook and SMTP (see *Turning payments on*).
- Put a reverse proxy in front for TLS and upload limits.
- Watch the disk. The server keeps each upload and result until the operator
  deletes it.
- Run workers on hardware that can take it. Alignment needs roughly 2–3 GB of
  RAM; a 1 GB VPS will OOM. A 4h40m Russian audiobook took ~45 minutes on
  `tiny`/CPU with 15 threads here, and would take many hours on one vCPU.

---

## Performance

Alignment is dominated by the Whisper pass. `tiny` on CPU is the slow path; a
GPU with `--device cuda` is far faster. Chaptered files are processed chapter by
chapter, which is also what makes the progress bar meaningful — the runner
counts chapter completions rather than trusting the per-chapter bar, which
restarts at 0% for every chapter.

Single files longer than about four hours can exhaust RAM, per upstream. Prefer
chaptered `m4b`, or a folder of per-chapter files.

## Licence

AGPL-3.0: see `LICENSE`. You may use, change and host this, and you must give
your users the source of what you host. `NOTICE` has the licences of the work
this is built on (SubPlz, whisper.cpp, Whisper).
