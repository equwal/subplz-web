# SubPlz Web

Line an audiobook up with its ebook, sentence by sentence.

Drop in an audiobook and the ebook it was read from. You get back subtitles
timed to the narration, with the wording taken from your own book rather than
from a machine's guess at what it heard. Two things to do with that:

- **HoshiReader whispersync** — the `.srt` is the timing file.
- **Subtitled video** — a YouTube-ready MP4 with a selectable caption track.

One free book per rolling 24 hours; see [Pricing](#pricing).

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

**Book** — `epub`, `txt`, `srt`, `vtt`, `ass` directly; `fb2`, `fb2.zip`,
`mobi`, `azw3`, `azw` and `prc` are converted on upload.

Conversion delegates rather than parsing ebook formats by hand
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
wasted hour.

So the app asks the same question on upload, using subplz's own rule —
transcribe a 60-second sample from the start of up to three chapters, score each
against every chapter of the book with `rapidfuzz.fuzz.ratio`, and compare
against subplz's `SCORE_THRESHOLD = 40`. It costs a few seconds.

Measured on real files:

| Pair | Score | Verdict |
|---|---|---|
| Moskva-Petushki audio + its own epub | **74.4** | good |
| Moskva-Petushki audio + an unrelated Russian novel | **40.5** | poor |

That second row is the important one. An unrelated book in the same language
still clears subplz's threshold of 40, because two prose texts in one language
are roughly 40% similar character-by-character whatever they say. So the bands
here sit well above it: good at ≥60, marginal at ≥48, poor below. A poor score
warns and relabels the button "Start anyway" — it never blocks, because the
check only samples, and it is your book.

### What actually improves the score

Two things were tried and measured, and only one of them works.

**Chapter structure — decisive.** subplz compares the *opening* of each audio
chapter against the *opening* of each text chapter, capped at 2000 characters.
It splits an epub into one text chapter per spine document, but a `.txt` into
exactly one chapter for the whole file. Same book, same audio:

| Text given to subplz | Score |
|---|---|
| epub, per chapter | **69.1** |
| identical text as one flat file | **38.6** — *below the threshold* |

A flat text file turns a perfectly good book into a failed run. That is why
`convert.py` emits a chaptered epub rather than plain text, and why the app
warns when a one-document book is paired with multi-chapter audio.

**Typographic cleanup — no effect, so it was dropped.** Normalising curly
quotes, em dashes, soft hyphens, non-breaking spaces and footnote markers
measured **+0.0** against a real transcript. Joining paragraphs with a space
instead of subplz's empty string was worth +0.3. Both are noise next to
chapterisation, and shipping them would have been dead code. (This is the one
place it matters that `ats/lang.py` implements only Japanese and English —
every other language falls back to `English`, whose `clean()` is nothing but
`.lower()`, so punctuation is compared verbatim. It still does not move the
number.)

Disable the check with `SUBPLZ_WEB_MATCH_CHECK=false`.

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
| `backend/billing.py` | the 24-hour allowance |
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

Uploaded media is deleted once a job succeeds. A **failed** job keeps its inputs
so you can fix the language and retry without re-uploading.

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

One free book per rolling 24 hours, then paid. The window is rolling rather than
a calendar day: the allowance returns 24 hours after the run that used it.

| Plan | Price | Per book |
|---|---|---|
| Free | — | 1 per 24h |
| One book | $3.49 | $3.49 |
| 5 books | $12.99 | $2.60 |
| 20 books | $39.99 | $2.00 |
| Unlimited monthly | $14.99 | — |

Set against the market (2026): the direct competitors are cheap or free —
Voxlight $29.99/year (alignment runs on the user's own Mac), Storyteller and
syncabook free but self-hosted. The adjacent subtitling tools price for
*transcription* and do not transfer: Sonix is $10/hour, so a 10-hour audiobook
would be ~$100, and Happy Scribe's 120-minute $17 tier would not fit one book.
Forced alignment is far cheaper to run than transcription, because the model is
tiny and its output is thrown away. So: per book, priced as an impulse buy, with
the subscription just under Otter ($16.99) and Happy Scribe ($17).

Every number is an env var — see `backend/pricing.py`.

**Identity is a cookie, and only a cookie.** Anyone who clears it gets another
free book. That is accepted: hard verification means accounts, email and a
signup wall in front of a tool whose pitch is "drop two files in". The real
protection against abuse is capacity, not identity.

What is **not** included is a payment provider. `billing.start_checkout` raises
`NotImplementedError` and is the single place Stripe plugs in. Credit
`Account.purchased_credits` from the webhook, never from the success redirect.

---

## Scaling out

Everything environment-specific is an env var with a localhost default. See
`.env.example`.

| Concern | localhost | public |
|---|---|---|
| Queue | thread pool in the API process | Redis + `worker.py` |
| Storage | `./data/artifacts` | S3, presigned download URLs |
| Database | SQLite | Postgres |
| Identity | cookie | replace `api.get_account` |
| Billing | off | `SUBPLZ_WEB_BILLING_ENABLED=true` |

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

- Implement `billing.start_checkout` and its webhook.
- Put a reverse proxy in front for TLS and upload limits.
- Add a retention job — audiobooks are large and artifacts are kept forever.
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
