/* SubRead web UI. No framework, no build step - one file, served as-is. */

const $ = (id) => document.getElementById(id);

const el = {
  dropzone: $('dropzone'), picker: $('filepicker'), browse: $('browse'),
  uploading: $('uploading'), upbar: $('upbar'), uptext: $('uptext'),
  error: $('error'),
  confirm: $('confirm'), cAudio: $('c-audio'), cText: $('c-text'),
  language: $('language'), detected: $('detected'), splitnote: $('splitnote'),
  start: $('start'), discard: $('discard'), eta: $('eta'),
  inBrowser: $('in-browser'), modeNote: $('mode-note'),
  jobsSection: $('jobs-section'), jobs: $('jobs'), quota: $('quota'),
  freetier: $('freetier'), staged: $('staged'), dzTitle: $('dz-title'),
  match: $('match'), matchBadge: $('match-badge'),
  matchSummary: $('match-summary'), matchWarnings: $('match-warnings'),
  who: $('who'), buyBtn: $('buy-btn'), portalBtn: $('portal-btn'),
  signinBtn: $('signin-btn'), signoutBtn: $('signout-btn'),
  signinDialog: $('signin-dialog'), signinForm: $('signin-form'),
  signinEmail: $('signin-email'), signinSend: $('signin-send'),
  signinNote: $('signin-note'),
  pricingDialog: $('pricing-dialog'), pricingWhy: $('pricing-why'),
  pricingSignin: $('pricing-signin'), pricingSigninBtn: $('pricing-signin-btn'),
  plans: $('plans'), toast: $('toast'),
  working: $('working'), workTitle: $('work-title'), workBar: $('work-bar'),
  workMeta: $('work-meta'), stop: $('stop'), results: $('results'),
  resultFiles: $('result-files'), another: $('another'),
};

let languages = [];
let languagesReady = null;  // resolves once the <select> is populated
let draft = null;           // the job awaiting confirmation
let pollTimer = null;
let account = null;         // last /api/account response

/* ---------------- helpers ---------------- */

const fmtBytes = (n) => {
  if (!n) return '';
  const u = ['B', 'KB', 'MB', 'GB'];
  let i = 0, v = n;
  while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
  return `${v < 10 && i > 0 ? v.toFixed(1) : Math.round(v)} ${u[i]}`;
};

const fmtDuration = (s) => {
  if (!s && s !== 0) return '';
  const h = Math.floor(s / 3600), m = Math.round((s % 3600) / 60);
  return h ? `${h}h ${String(m).padStart(2, '0')}m` : `${m}m`;
};

function showError(msg) {
  el.error.textContent = msg;
  el.error.hidden = false;
  el.error.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}
const clearError = () => { el.error.hidden = true; };

async function api(path, opts = {}) {
  const res = await fetch(path, { credentials: 'same-origin', ...opts });
  if (!res.ok) {
    let detail = `${res.status} ${res.statusText}`;
    try {
      const body = await res.json();
      if (body.detail) detail = typeof body.detail === 'string'
        ? body.detail : JSON.stringify(body.detail);
    } catch { /* non-JSON error body */ }
    const err = new Error(detail);
    err.status = res.status;
    throw err;
  }
  return res.status === 204 ? null : res.json();
}

/* ---------------- setup ---------------- */

async function loadLanguages() {
  // Whisper's own list: the speech model is the only thing that cares.
  ({ LANGUAGES: languages } = await import('/engine/languages.js'));
  el.language.innerHTML = '';
  for (const l of languages) {
    const o = document.createElement('option');
    o.value = l.code;
    o.textContent = `${l.name} (${l.code})`;
    el.language.appendChild(o);
  }
}

/* Setting .value on a <select> silently does nothing when the option is not
   there yet, which would leave whatever was selected before on screen while the
   badge says something else. Make the mismatch impossible instead. */
function setLanguage(code) {
  el.language.value = code;
  if (el.language.value !== code) {
    const o = document.createElement('option');
    o.value = code;
    o.textContent = code;
    el.language.appendChild(o);
    el.language.value = code;
  }
}

async function refreshAccount() {
  try { account = await api('/api/account'); }
  catch { return; /* cosmetic; never block the UI on it */ }
  renderAccount();
}

function renderAccount() {
  const a = account;
  if (!a) return;
  signedIn = !!a.signed_in;

  const hint = document.getElementById('cover-hint');
  if (hint) {
    hint.innerHTML = signedIn
      ? 'Optional: drop a jpg or png to use as the video background'
      : 'Optional: drop a jpg or png to use as the video background — ' +
        '<strong>sign in required</strong>';
  }
  if (a.free_tier_summary && el.freetier) {
    el.freetier.textContent =
      a.free_tier_summary.replace(/^./, (c) => c.toUpperCase()) + '.';
  }

  el.who.hidden = !signedIn;
  el.who.textContent = signedIn ? a.email : '';
  el.signoutBtn.hidden = !signedIn;
  // A buyer has an email from Stripe that is not verified yet. The same
  // sign-in link verifies it.
  el.signinBtn.hidden = !a.email_sign_in_available || (signedIn && a.email_verified);
  const offer = signInOffer();
  const verb = signedIn ? 'Verify your email' : 'Sign in';
  el.signinBtn.textContent = offer ? `${verb} for ${offer}` : verb;

  // What is sold is conversion on this server's hardware. Nothing is for sale
  // until that is connected; in this tab each output is free.
  const selling = a.billing_enabled && a.cloud_available;
  // A monthly plan with a limit can still add a pack; an unlimited one needs none.
  el.buyBtn.hidden = !selling || a.unlimited;
  el.portalBtn.hidden = !(selling && a.subscribed);
  el.quota.hidden = !selling;
  if (selling) {
    // Say "free" while each credit in hand is a free one.
    const free = a.credits > 0 && a.free_credits === a.credits ? 'free ' : '';
    el.quota.textContent = a.unlimited ? 'Unlimited plan'
      : `${a.credits} ${free}cloud credit${a.credits === 1 ? '' : 's'}`;
  }
  renderMode();
}

/* What a verified email gives this visitor, in words: "2 free credits + 1 a
   day". Empty when there is nothing to offer: the email is verified, the
   server sends no sign-in email, or billing is off. */
function signInOffer() {
  const a = account;
  if (!a || a.email_verified || !a.email_sign_in_available || !a.billing_enabled) return '';
  const n = a.free_credits_with_account || 0;
  const parts = [];
  if (n) parts.push(`${n} free credit${n === 1 ? '' : 's'}`);
  if (a.daily_free_credit) parts.push(n ? '1 a day' : '1 free credit a day');
  return parts.join(' + ');
}

let toastTimer = null;
function toast(msg, ms = 6000) {
  el.toast.textContent = msg;
  el.toast.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.toast.hidden = true; }, ms);
}

/* ---------------- upload ---------------- */

/* Files can arrive one drop at a time, so collect them until both halves are
   here, then upload. Ignores the cover art and readme files that ride along
   inside audiobook folders. */
const AUDIO_RE = /\.(m4b|m4a|mp3|opus|ogg|oga|flac|wav|aac|wma|mka|mkv|mp4|webm|avi|mov)$/i;
const TEXT_RE = /(\.fb2\.zip|\.(epub|txt|srt|vtt|ass|fb2|mobi|azw|azw3|prc))$/i;
const IMAGE_RE = /\.(jpe?g|png|webp|bmp)$/i;

const staged = { audio: [], text: null, cover: null };
let signedIn = false;

function addFiles(files) {
  clearError();
  const list = [...files];
  const audio = list.filter((f) => AUDIO_RE.test(f.name));
  const text = list.filter((f) => TEXT_RE.test(f.name));
  const images = list.filter((f) => IMAGE_RE.test(f.name));

  // Say up front that a cover needs an account, rather than accepting the file
  // and springing it on them after they have waited for a render.
  if (images.length && !signedIn) {
    showError('Using your own cover image needs an account — sign in first, ' +
              'and it will be applied to the video. The rest works without one.');
  } else if (images.length) {
    staged.cover = images[images.length - 1];
  }

  if (!audio.length && !text.length && !images.length) {
    showError('Nothing usable in that drop — expected an audiobook or an ebook.');
    return;
  }

  // Audio accumulates, so a per-chapter set can arrive in batches. Only one
  // book makes sense, so a second one replaces the first.
  for (const f of audio) {
    if (!staged.audio.some((g) => g.name === f.name && g.size === f.size)) {
      staged.audio.push(f);
    }
  }
  if (text.length) staged.text = text[text.length - 1];

  renderStaged();
  if (!audio.length && !text.length) return;  // an image alone is not a job
  if (staged.audio.length && staged.text) {
    prepare();
  }
}

function renderStaged() {
  const have = staged.audio.length || staged.text;
  el.staged.hidden = !have;

  const a = $('slot-audio');
  a.classList.toggle('filled', staged.audio.length > 0);
  a.querySelector('.slot-value').textContent = staged.audio.length
    ? (staged.audio.length > 1
        ? `${staged.audio[0].name} + ${staged.audio.length - 1} more`
        : staged.audio[0].name)
    : 'Waiting for an audio file';

  const cv = $('slot-cover');
  cv.hidden = !staged.cover;
  if (staged.cover) {
    cv.classList.add('filled');
    cv.querySelector('.slot-value').textContent = staged.cover.name;
  }

  const t = $('slot-text');
  t.classList.toggle('filled', !!staged.text);
  t.querySelector('.slot-value').textContent =
    staged.text ? staged.text.name : 'Waiting for an ebook';

  // Name the missing half, but only once one half is actually here.
  const missing = !have ? null
    : !staged.audio.length ? 'audiobook'
    : !staged.text ? 'ebook'
    : null;
  el.dzTitle.innerHTML = missing
    ? `Now drop the <em>${missing}</em>`
    : 'Drop your audiobook <em>and</em> ebook here';
}

function clearStaged() {
  staged.audio = [];
  staged.text = null;
  staged.cover = null;
  renderStaged();
}

document.querySelectorAll('.slot-x').forEach((btn) => {
  btn.addEventListener('click', () => {
    if (btn.dataset.slot === 'audio') staged.audio = [];
    else if (btn.dataset.slot === 'cover') staged.cover = null;
    else staged.text = null;
    renderStaged();
  });
});

/* Nothing is uploaded. Once both halves are here the book is read in this tab,
   its language guessed, and the visitor asked to confirm before hours of work. */
const natural = new Intl.Collator(undefined, { numeric: true, sensitivity: 'base' });

async function prepare() {
  clearError();
  el.dropzone.hidden = true;
  el.staged.hidden = true;
  el.confirm.hidden = true;
  el.uploading.hidden = false;
  el.upbar.style.width = '100%';
  el.uptext.textContent = 'Reading the book…';

  try {
    const { readBook, detectLanguage } = await import('/engine/book.js');
    const book = staged.text;
    const parsed = await readBook(book);
    if (!parsed.paragraphs.length) {
      throw new Error(`No text could be read from ${book.name}. A scanned, image-only book cannot be aligned.`);
    }
    // 9.mp3 before 10.mp3: playback order is what the timeline is built from.
    const audio = staged.audio.slice().sort((a, b) => natural.compare(a.name, b.name));
    draft = { audio, book, cover: staged.cover, detected: detectLanguage(parsed.paragraphs) };
    el.uploading.hidden = true;
    showConfirm();
  } catch (e) {
    el.uploading.hidden = true;
    showError(e.message);
    resetToDrop();
  }
}

function resetToDrop() {
  draft = null;
  clearStaged();
  el.dropzone.hidden = false;
  el.confirm.hidden = true;
  el.uploading.hidden = true;
  el.working.hidden = true;
  el.picker.value = '';
}

/* ---------------- confirm ---------------- */

async function showConfirm() {
  const bytes = draft.audio.reduce((n, f) => n + f.size, 0);
  const name = draft.audio.length > 1
    ? `${draft.audio[0].name} + ${draft.audio.length - 1} more` : draft.audio[0].name;
  el.cAudio.innerHTML = `${escapeHtml(name)} <span class="meta">${fmtBytes(bytes)}</span>`;
  el.cText.textContent = draft.book.name;

  if (draft.detected) {
    setLanguage(draft.detected);
    el.detected.textContent = 'detected from the book';
    el.detected.classList.remove('low');
  } else {
    el.detected.textContent = 'could not tell — please choose';
    el.detected.classList.add('low');
  }
  el.detected.hidden = false;
  el.match.hidden = true;

  // Only the speech model decides how long this takes, and only the GPU
  // decides how fast the speech model is. Say which it will be.
  const { hasWebGpu } = await import('/engine/asr.js');
  const gpu = await hasWebGpu();
  const { savedProgress } = await import('/engine/job.js');
  const saved = await savedProgress(draft.audio, el.language.value);
  browserPlan = {
    note: (saved?.complete ? 'This audio was transcribed here before, so this will take seconds. '
      : saved?.doneUntil ? `Picks up where it stopped, ${fmtDuration(saved.doneUntil)} in. ` : '') +
      'Everything runs in this tab, and it has to stay open. ' +
      (saved?.complete ? '' : gpu ? 'Expect roughly a quarter of the book\'s length.'
        : 'This browser has no WebGPU, so expect about the book\'s own length — Chrome or Edge on a computer with a graphics card is several times faster.'),
    label: saved?.doneUntil && !saved.complete ? 'Continue' : 'Start',
  };

  el.confirm.hidden = false;
  el.start.disabled = false;
  renderMode();
  el.confirm.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}

el.discard.addEventListener('click', () => { clearError(); resetToDrop(); });

/* ---------------- where the work is done ---------------- */

/* In this browser (JavaScript, free, nothing uploaded) is the default. The
   switch is above the drop zone, so the choice shows before a file is chosen,
   and each visit starts with the switch on. Off sends the job to the server:
   for a phone, an old computer, or a tab that has to close. */
let browserPlan = { note: '', label: 'Start' };

function serverOffered() { return !!account?.cloud_available; }

function inBrowser() { return !serverOffered() || el.inBrowser.checked; }

/* What a job on the server costs this visitor, as a sentence. */
function serverPrice() {
  const a = account;
  if (!a?.billing_enabled) return '';
  if (a.unlimited) return ' Included in your plan.';
  if (a.credits > 0) {
    const free = a.free_credits === a.credits ? ' free' : '';
    return ` One credit a book; you have ${a.credits}${free}.`;
  }
  return ' One credit a book.';
}

/* The same cost as a clause, for the note of the browser mode: the price must
   stay with the server, or it reads as if the browser mode took a credit. */
function serverCost() {
  const a = account;
  if (!a?.billing_enabled) return '';
  if (a.unlimited) return ', which your plan includes';
  return `, for one credit a book${a.credits > 0 ? ` (you have ${a.credits})` : ''}`;
}

function renderMode() {
  const offered = serverOffered();
  el.inBrowser.disabled = !offered;
  if (!offered) el.inBrowser.checked = true;
  if (inBrowser()) {
    const copy = account?.browser_copy
      ? ' During the beta, a copy of your files goes to our server, for debugging.' : '';
    el.modeNote.textContent = 'Uses no credit, and has no limit.' + copy +
      (offered ? ` Turn this off to have our server do the work instead${serverCost()}.` : '');
    el.start.textContent = browserPlan.label;
    el.eta.textContent = browserPlan.note +
      (offered ? '' : ' Our server is not taking conversions at the moment, so this is the one way.');
    return;
  }
  el.modeNote.textContent = 'Off: your files are uploaded, and our server does the work.' + serverPrice();
  el.start.textContent = 'Upload and convert on our server';
  el.eta.textContent =
    'The files are uploaded, the work is done on our server, and the result waits for you on this page, ' +
    'so you can close this tab. We keep the files on our server to find faults, and delete them on request.' +
    serverPrice();
}

el.inBrowser.addEventListener('change', renderMode);

function startOnServer() {
  if (!draft || running) return;
  clearError();
  // Ask for the credit before the upload, not after an hour of it.
  if (account.billing_enabled && !account.cloud_allowed) {
    openPricing('A conversion on our server takes one credit. In this tab it is free, without limit.');
    return;
  }
  const { audio, book, cover } = draft;
  const language = el.language.value;
  el.confirm.hidden = true;
  el.uploading.hidden = false;
  el.upbar.style.width = '0%';
  el.uptext.textContent = 'Starting…';

  const form = new FormData();
  for (const f of [...audio, book]) form.append('files', f, f.name);
  if (cover && signedIn) form.append('files', cover, cover.name);

  const back = (message) => { el.uploading.hidden = true; el.confirm.hidden = false; showError(message); };
  // XHR and not fetch: it reports upload progress, and these files are big.
  const xhr = new XMLHttpRequest();
  xhr.open('POST', '/api/uploads');
  xhr.withCredentials = true;
  xhr.upload.onprogress = (e) => {
    if (!e.lengthComputable) return;
    const pct = Math.round((e.loaded / e.total) * 100);
    el.upbar.style.width = `${pct}%`;
    el.uptext.textContent = pct < 100 ? `${pct}% — ${fmtBytes(e.loaded)} of ${fmtBytes(e.total)}` : 'Checking the book…';
  };
  xhr.onerror = () => back('The upload failed: the connection to the server was lost.');
  xhr.onload = async () => {
    let body = null;
    try { body = JSON.parse(xhr.responseText); } catch { /* handled below */ }
    if (xhr.status < 200 || xhr.status >= 300 || !body?.job) {
      back(typeof body?.detail === 'string' ? body.detail : `The upload failed (${xhr.status}).`);
      return;
    }
    try {
      await api(`/api/jobs/${body.job.id}/start`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ language }),
      });
    } catch (e) {
      // The files are on the server but nothing runs: do not leave them there.
      api(`/api/jobs/${body.job.id}`, { method: 'DELETE' }).catch(() => {});
      if (e.status === 402) { el.uploading.hidden = true; el.confirm.hidden = false; openPricing(e.message); }
      else back(e.message);
      return;
    }
    resetToDrop();
    toast('The server has the book. You can close this tab; the result will be under "Your conversions" on this page.', 12000);
    refreshJobs();
    refreshAccount();
  };
  xhr.send(form);
}

/* ---------------- the job, in this tab ---------------- */

let running = null;    // { job, serverId }
let wakeLock = null;

el.start.addEventListener('click', async () => {
  if (!draft || running) return;
  if (!inBrowser()) { startOnServer(); return; }
  el.start.disabled = true;
  clearError();
  const language = el.language.value;

  // The server keeps the list of this visitor's books, and nothing more: a job
  // in this tab costs nothing and needs nothing from it. A server that is down
  // or missing only loses the history entry.
  const registered = await api('/api/local/jobs', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      audio_filename: draft.audio.length > 1 ? `${draft.audio[0].name} + ${draft.audio.length - 1} more` : draft.audio[0].name,
      audio_parts: draft.audio.length,
      audio_bytes: draft.audio.reduce((n, f) => n + f.size, 0),
      text_filename: draft.book.name, language,
    }),
  }).catch(() => null);
  if (registered && account?.browser_copy) sendCopy(registered.id, draft.audio, draft.book);
  const report = (path, body) => (registered
    ? api(`/api/local/jobs/${registered.id}/${path}`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
    }).catch(() => {})
    : Promise.resolve());

  const { Job, Cancelled } = await import('/engine/job.js');
  const job = new Job({ audio: draft.audio, book: draft.book, language, onStatus: renderWorking });
  running = { job, serverId: registered?.id, cover: draft.cover };
  el.confirm.hidden = true;
  el.working.hidden = false;
  el.results.hidden = true;
  el.stop.hidden = false;
  renderWorking({ phase: 'preparing', detail: 'Starting', fraction: 0 });
  refreshAccount();
  try { wakeLock = await navigator.wakeLock?.request('screen'); } catch { /* not granted: fine */ }

  try {
    const result = await job.run();
    // The subtitles are here to download whatever the server says; only the history entry can go missing.
    await report('finish', {
      srt: result.srt, filename: result.srtName,
      metadata: {
        source: { audio_parts: draft.audio.length, text_filename: draft.book.name,
                  audio_duration_seconds: result.duration },
        alignment: { where: 'in the browser', device: job.device ?? 'cached transcript', model: 'whisper-tiny',
                     language, mode: 'forced alignment against supplied text' },
        output: { filename: result.srtName, cue_count: result.cues.length,
                  match_rate: result.matchRate, paragraphs_dropped: result.paragraphsDropped },
      },
    });
    showResults(result);
  } catch (e) {
    const stopped = e instanceof Cancelled;
    await report('fail', { error: stopped ? 'Stopped.' : String(e.message || e) });
    job.close();
    running = null;
    el.working.hidden = true;
    if (stopped) { toast('Stopped. What was transcribed is kept — the same files will carry on from there.'); el.confirm.hidden = false; el.start.disabled = false; }
    else { showError(e.message || String(e)); resetToDrop(); }
  } finally {
    wakeLock?.release?.().catch(() => {});
    refreshAccount();
    refreshJobs();
  }
});

el.stop.addEventListener('click', () => { running?.job.cancel(); el.stop.disabled = true; });

/* During the beta, a copy of the audio and the book goes to the server, for
   debugging, as the privacy and user data policy says. It runs beside the job
   and costs no credit. If it fails, nothing changes for the visitor. */
function sendCopy(jobId, audio, book) {
  const form = new FormData();
  for (const f of [...audio, book]) form.append('files', f, f.name);
  fetch(`/api/local/jobs/${jobId}/files`, { method: 'POST', body: form, credentials: 'same-origin' })
    .catch(() => { /* a copy for debugging only */ });
}

function renderWorking(st) {
  el.workTitle.textContent = st.detail;
  el.workBar.style.width = `${Math.round((st.fraction || 0) * 100)}%`;
  el.workBar.parentElement.classList.toggle('indeterminate', !!st.indeterminate);
  el.workMeta.textContent = [
    `${Math.round((st.fraction || 0) * 100)}%`,
    st.eta != null ? `about ${fmtDuration(st.eta)} left` : null,
    st.speed ? `${st.speed.toFixed(1)}× real time` : null,
  ].filter(Boolean).join('  ·  ');
}

function showResults(r) {
  // The ask goes to people who got a book for nothing, at the moment it worked.
  // Not to a customer: someone who pays for credits has done their part.
  // Free credits do not make a customer, so count the bought ones only.
  const chipIn = document.getElementById('chip-in');
  const bought = (account?.credits ?? 0) - (account?.free_credits ?? 0)
    - (account?.plan_credits ?? 0);
  if (chipIn) chipIn.hidden = Boolean(bought > 0 || account?.subscribed);
  el.stop.hidden = true;
  el.stop.disabled = false;
  el.results.hidden = false;
  const pct = Math.round(r.matchRate * 100);
  el.workTitle.textContent = `Done — ${r.cues.length} lines, ${pct}% found in the book`;
  el.workBar.style.width = '100%';
  el.workMeta.textContent =
    (pct < 80 ? 'That is low: usually a different edition or translation, or the wrong language. ' : '') +
    (r.paragraphsDropped ? `${r.paragraphsDropped} paragraphs of the book were never narrated (front matter, notes) and were left out.` : '');
  renderResultButtons();
}

function renderResultButtons() {
  const r = running.job.result;
  el.resultFiles.innerHTML = '';
  const button = (label, hint, cls, onClick) => {
    const b = document.createElement('button');
    b.type = 'button'; b.className = `dl ${cls}`; b.title = hint; b.textContent = label;
    b.addEventListener('click', () => onClick(b));
    el.resultFiles.appendChild(b);
  };
  button('⬇ Subtitles (.srt)', 'For Hoshi Reader, or to upload alongside the YouTube video', '',
    () => save(new File([r.srt], r.srtName, { type: 'application/x-subrip' })));
  if (/\.epub$/i.test(running.job.bookFile.name)) {
    button('⬇ Read-along book (.epub)', 'The book with the narration inside: Thorium, Storyteller and other EPUB 3 readers highlight each line as it is read', '',
      (b) => makeEpub(b));
  }
  button('⬇ Video with subs built in (.mkv)', 'Subtitles inside the file, for MPV or VLC', '',
    (b) => makeVideo('mkv', b));
  button('⬇ Video for YouTube (.mp4)', 'No subtitles baked in — add the .srt in YouTube Studio', '',
    (b) => makeVideo('mp4', b));
  // honjimaku is a library for Japanese books.
  if (running.job.language === 'ja') {
    button('⇪ Share on honjimaku', 'Give the subtitles to honjimaku.com, a free library for Japanese audiobooks', 'secondary',
      () => openShare());
  }
}

/* ---------------- share on honjimaku ---------------- */

// A test can point this at a local honjimaku.
const honjimaku = () => window.SUBREAD_HONJIMAKU || 'https://honjimaku.com';
const shareEl = {
  dialog: $('share-dialog'), form: $('share-form'), title: $('share-title'), bookId: $('share-book-id'),
  key: $('share-key'), send: $('share-send'), status: $('share-status'),
};

function openShare() {
  const r = running.job.result;
  shareEl.title.value = shareEl.title.value || r.stem;
  try { shareEl.key.value = localStorage.getItem('honjimaku-key') || ''; } catch { /* private window */ }
  shareEl.status.hidden = true;
  shareEl.dialog.showModal();
}

shareEl.form.addEventListener('submit', async (e) => {
  e.preventDefault();
  const r = running?.job.result;
  if (!r) return;
  const say = (text, link) => {
    shareEl.status.hidden = false;
    shareEl.status.textContent = text;
    if (link) {
      const a = document.createElement('a');
      a.href = link; a.target = '_blank'; a.rel = 'noopener'; a.textContent = ' Open it on honjimaku.';
      shareEl.status.append(a);
    }
  };
  const key = shareEl.key.value.trim();
  const call = async (path, options) => {
    const res = await fetch(honjimaku() + path, { ...options, credentials: 'omit', headers: { Authorization: key, ...(options.headers || {}) } });
    const body = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(body.error || `honjimaku answered ${res.status}.`);
    return body;
  };
  shareEl.send.disabled = true;
  say('Sending…');
  try {
    // Finds the book if honjimaku has it, however the title is typed, or makes it.
    const bookId = shareEl.bookId.value.trim();
    const { entry_id: entry } = await call('/api/entries', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name: shareEl.title.value.trim(), ...(bookId ? { book_id: bookId } : {}) }),
    });
    const form = new FormData();
    form.append('file', new File([r.srt], r.srtName, { type: 'application/x-subrip' }));
    const done = await call(`/api/entries/${entry}/upload`, { method: 'POST', body: form });
    try { localStorage.setItem('honjimaku-key', key); } catch { /* private window */ }
    if (done.problems?.length) say(`honjimaku did not take the file. ${done.problems.join(' ')}`);
    else say('Shared. Thank you.', `${honjimaku()}/entry/${entry}`);
  } catch (err) {
    say(err.message === 'Failed to fetch' ? 'honjimaku.com could not be reached.' : err.message);
  }
  shareEl.send.disabled = false;
});

async function makeEpub(btn) {
  const label = btn.textContent;
  btn.disabled = true;
  btn.textContent = 'Making the read-along book…';
  try {
    const { file, located, of } = await running.job.epub();
    save(file);
    if (located < of) toast(`${of - located} of ${of} lines could not be placed in the book's pages; the rest are in.`);
  } catch (e) { showError(`Could not make the read-along book: ${e.message}`); }
  btn.textContent = label;
  btn.disabled = false;
}

async function makeVideo(kind, btn) {
  const label = btn.textContent;
  btn.disabled = true;
  try {
    const file = await running.job.video(kind, {
      coverFile: signedIn ? running.cover : null,
      onProgress: (f) => { btn.textContent = `Making the ${kind}… ${Math.round(f * 100)}%`; },
    });
    save(file);
  } catch (e) { showError(`Could not make the ${kind}: ${e.message}`); }
  btn.textContent = label;
  btn.disabled = false;
}

function save(file) {
  const a = document.createElement('a');
  a.href = URL.createObjectURL(file);
  a.download = file.name;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 60_000);
}

el.another.addEventListener('click', () => { running?.job.close(); running = null; resetToDrop(); });

// Hours of work live in this tab. Make closing it a decision, not an accident.
window.addEventListener('beforeunload', (e) => {
  if (running && !running.job.result) { e.preventDefault(); e.returnValue = ''; }
});

/* ---------------- jobs ---------------- */

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  ));
}

function jobCard(j) {
  const li = document.createElement('li');
  li.className = 'job';

  // A browser-run job reports no progress to the server; it is either ours
  // (shown above, in the working panel) or running in some other tab.
  const active = !j.local && (j.status === 'running' || j.status === 'queued');
  const pct = Math.round(j.progress * 100);

  const meta = [
    j.language_name,
    j.audio_parts > 1 ? `${j.audio_parts} parts` : null,
    fmtDuration(j.audio_duration_seconds),
    fmtBytes(j.audio_bytes),
  ].filter(Boolean).join(' · ');

  let html = `
    <div class="job-head">
      <div style="min-width:0">
        <div class="job-name">${escapeHtml(j.audio_filename)}</div>
        <div class="job-sub">${escapeHtml(meta)}</div>
      </div>
      <span class="pill ${j.status}">${j.status}</span>
    </div>`;

  if (active) {
    html += `
      <div class="bar"><div class="bar-fill" style="width:${pct}%"></div></div>
      <div class="job-stage"><span>${escapeHtml(j.stage)}</span><span>${pct}%</span></div>`;
  }

  if (j.local && j.status === 'running') {
    html += '<div class="job-stage"><span>Running in a browser tab</span></div>';
  }
  if (j.error) html += `<div class="job-err">${escapeHtml(j.error)}</div>`;

  const files = j.artifacts || [];
  if (files.length) {
    const order = { srt: 0, video: 1, video_embedded: 2, metadata: 3, log: 4 };
    // Three deliverables for three different uses; say which is which, because
    // "two video files" is otherwise baffling.
    const label = {
      srt: '⬇ Subtitles (.srt)',
      video: '⬇ Video for YouTube (.mp4)',
      video_embedded: '⬇ Video with subs built in (.mkv)',
      metadata: 'Metadata',
      log: 'Run log',
    };
    const hint = {
      srt: 'For HoshiReader, or upload alongside the YouTube video',
      video: 'No subtitles baked in — add the .srt in YouTube Studio',
      video_embedded: 'Subtitles inside the file, for MPV/VLC',
    };
    const primary = new Set(['srt', 'video', 'video_embedded']);
    html += '<div class="job-files">' + files
      .slice()
      .sort((a, b) => (order[a.kind] ?? 9) - (order[b.kind] ?? 9))
      .map((a) => {
        const size = a.size_bytes ? ` <span class="dl-size">${fmtBytes(a.size_bytes)}</span>` : '';
        const t = hint[a.kind] ? ` title="${escapeHtml(hint[a.kind])}"` : '';
        return `<a class="dl ${primary.has(a.kind) ? '' : 'secondary'}"${t}
                   href="${a.url}" download>${label[a.kind] || a.kind}${size}</a>`;
      })
      .join('') + '</div>';
  }

  li.innerHTML = html;

  if (active) {
    const cancel = document.createElement('button');
    cancel.className = 'ghost';
    cancel.style.marginTop = '13px';
    cancel.textContent = 'Cancel';
    cancel.onclick = async () => {
      cancel.disabled = true;
      try { await api(`/api/jobs/${j.id}/cancel`, { method: 'POST' }); }
      catch (e) { showError(e.message); }
      refreshJobs(); refreshAccount();
    };
    li.appendChild(cancel);
  }
  return li;
}

async function refreshJobs() {
  let jobs;
  try { jobs = await api('/api/jobs'); }
  catch { return; }

  // Drafts live in the confirm panel, not the list.
  const visible = jobs.filter((j) => j.status !== 'draft' && j.id !== running?.serverId);
  el.jobsSection.hidden = visible.length === 0;
  el.jobs.innerHTML = '';
  for (const j of visible) el.jobs.appendChild(jobCard(j));

  const anyActive = visible.some((j) => !j.local && (j.status === 'running' || j.status === 'queued'));
  clearTimeout(pollTimer);
  if (anyActive) pollTimer = setTimeout(refreshJobs, 1500);
}

/* ---------------- drag & drop ---------------- */

['dragenter', 'dragover'].forEach((ev) =>
  el.dropzone.addEventListener(ev, (e) => {
    e.preventDefault();
    el.dropzone.classList.add('drag');
  }));

['dragleave', 'drop'].forEach((ev) =>
  el.dropzone.addEventListener(ev, (e) => {
    e.preventDefault();
    if (ev === 'dragleave' && el.dropzone.contains(e.relatedTarget)) return;
    el.dropzone.classList.remove('drag');
  }));

/* Dropping a folder gives directory entries, not files - audiobooks often
   arrive as a folder of per-chapter mp3s, so walk it. */
function readEntries(reader) {
  return new Promise((resolve, reject) => reader.readEntries(resolve, reject));
}

async function walkEntry(entry, out, depth = 0) {
  if (!entry || depth > 4) return;
  if (entry.isFile) {
    out.push(await new Promise((res, rej) => entry.file(res, rej)));
    return;
  }
  if (entry.isDirectory) {
    const reader = entry.createReader();
    // readEntries returns at most 100 per call, so keep going until it is empty.
    for (;;) {
      const batch = await readEntries(reader);
      if (!batch.length) break;
      for (const child of batch) await walkEntry(child, out, depth + 1);
    }
  }
}

el.dropzone.addEventListener('drop', async (e) => {
  const items = e.dataTransfer?.items;
  const hasEntries = items && [...items].some(
    (i) => i.kind === 'file' && typeof i.webkitGetAsEntry === 'function'
  );

  if (hasEntries) {
    const entries = [...items]
      .filter((i) => i.kind === 'file')
      .map((i) => i.webkitGetAsEntry())
      .filter(Boolean);
    if (entries.some((en) => en.isDirectory)) {
      el.dropzone.hidden = true;
      el.uploading.hidden = false;
      el.uptext.textContent = 'Reading folder…';
      const files = [];
      try {
        for (const en of entries) await walkEntry(en, files);
      } catch (err) {
        el.uploading.hidden = true;
        showError(`Could not read the dropped folder: ${err.message}`);
        resetToDrop();
        return;
      }
      el.uploading.hidden = true;
      el.dropzone.hidden = false;
      if (files.length) addFiles(files);
      return;
    }
  }

  const files = e.dataTransfer?.files;
  if (files?.length) addFiles(files);
});

// Dropping anywhere else must not make the browser navigate to the file.
['dragover', 'drop'].forEach((ev) =>
  window.addEventListener(ev, (e) => e.preventDefault()));

el.dropzone.addEventListener('click', (e) => {
  if (e.target !== el.browse) el.picker.click();
});
el.browse.addEventListener('click', (e) => { e.stopPropagation(); el.picker.click(); });
el.dropzone.addEventListener('keydown', (e) => {
  if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); el.picker.click(); }
});
el.picker.addEventListener('change', () => {
  if (el.picker.files.length) addFiles(el.picker.files);
  el.picker.value = '';  // let the same file be chosen again after a removal
});

/* ---------------- checkout ---------------- */

async function openPricing(why) {
  el.pricingWhy.textContent = why ||
    'In this tab a conversion is free, without limit. A credit converts a book on our server: from any device, and you can close the tab.';
  const offer = signInOffer();
  el.pricingSignin.hidden = !offer;
  el.pricingSigninBtn.textContent = signedIn
    ? `Or verify your email and get ${offer}.`
    : `Or sign in with your email and get ${offer}.`;
  el.plans.innerHTML = '<p class="muted">Loading…</p>';
  if (!el.pricingDialog.open) el.pricingDialog.showModal();

  let catalogue;
  try { catalogue = await api('/api/pricing'); }
  catch (e) { el.plans.innerHTML = ''; showError(e.message); return; }

  el.plans.innerHTML = '';
  for (const p of catalogue.plans) {
    const card = document.createElement('div');
    card.className = 'plan' + (p.id === 'pack10' ? ' featured' : '');
    const per = p.per_book_cents && p.credits > 1
      ? `<span class="plan-per">$${(p.per_book_cents / 100).toFixed(2)} a book</span>` : '';
    card.innerHTML = `
      <div class="plan-name">${escapeHtml(p.name)}</div>
      <div class="plan-price">${escapeHtml(p.price_display)}${p.recurring ? '<small>/month</small>' : ''}</div>
      ${per}
      <p class="plan-blurb">${escapeHtml(p.blurb)}</p>`;
    const buy = document.createElement('button');
    buy.className = 'primary';
    buy.textContent = catalogue.payments_available
      ? (p.recurring ? 'Subscribe' : 'Buy') : 'Opening soon';
    buy.disabled = !catalogue.payments_available;
    buy.onclick = () => checkout(p.id, buy);
    card.appendChild(buy);
    el.plans.appendChild(card);
  }
}

async function checkout(planId, btn) {
  btn.disabled = true;
  try {
    const { url } = await api('/api/billing/checkout', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ plan_id: planId }),
    });
    if (running) {
      // The finished job only exists in this tab. Pay in another one.
      window.open(url, '_blank', 'noopener');
      el.pricingDialog.close();
      toast('Checkout opened in a new tab. Come back here when you have paid.', 12000);
      btn.disabled = false;
      return;
    }
    window.location.href = url;  // Stripe's page; we come back via /api/billing/return
  } catch (e) {
    el.pricingDialog.close();
    showError(e.message);
    btn.disabled = false;
  }
}

el.buyBtn.addEventListener('click', () => openPricing(
  'One credit converts one book on our server: from any device, and you can close the tab.'));

el.portalBtn.addEventListener('click', async () => {
  try {
    const { url } = await api('/api/billing/portal', { method: 'POST' });
    window.location.href = url;
  } catch (e) { showError(e.message); }
});

/* ---------------- sign in ---------------- */

function openSignIn() {
  el.signinNote.hidden = true;
  // A buyer verifies the address that Stripe has.
  if (account?.email && !el.signinEmail.value) el.signinEmail.value = account.email;
  el.signinDialog.showModal();
  el.signinEmail.focus();
}

el.signinBtn.addEventListener('click', openSignIn);
el.pricingSigninBtn.addEventListener('click', () => { el.pricingDialog.close(); openSignIn(); });

el.signinForm.addEventListener('submit', async (e) => {
  e.preventDefault();
  el.signinSend.disabled = true;
  try {
    const r = await api('/api/auth/request', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ email: el.signinEmail.value }),
    });
    el.signinNote.hidden = false;
    if (r.dev_link) {
      // Localhost has no mail server, so the server hands the link back.
      el.signinNote.innerHTML =
        `No mail server here — <a href="${escapeHtml(r.dev_link)}">open your sign-in link</a>.`;
    } else {
      el.signinNote.textContent =
        `Link sent to ${r.email}. It works once and expires in 30 minutes.`;
    }
  } catch (err) {
    el.signinNote.hidden = false;
    el.signinNote.textContent = err.message;
  }
  el.signinSend.disabled = false;
});

el.signoutBtn.addEventListener('click', async () => {
  try { await api('/api/auth/signout', { method: 'POST' }); } catch {}
  window.location.replace('/');
});

/* The sign-in link and Stripe's return trip both land here with a query
   string. Act on it once, then clean the URL so a reload does not repeat it. */
async function handleArrival() {
  const q = new URLSearchParams(window.location.search);
  const token = q.get('login');
  const paid = q.get('checkout');
  if (!token && !paid) return;
  window.history.replaceState({}, '', '/');

  if (token) {
    try {
      account = await api('/api/auth/verify', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ token }),
      });
      toast(`Signed in as ${account.email}.`);
    } catch (e) { showError(e.message); }
  }
  if (paid === 'paid') toast('Payment received — thank you. Your credits are ready.');
  if (paid === 'pending') {
    toast('Payment is still being confirmed. Credits appear here as soon as it clears.', 9000);
  }
}

// Back from paying in the other tab.
window.addEventListener('focus', () => { if (running?.job.result) refreshAccount().catch(() => {}); });

/* ---------------- boot ---------------- */

(async function init() {
  languagesReady = loadLanguages();
  try { await languagesReady; }
  catch (e) { showError(`Could not reach the server: ${e.message}`); }
  await handleArrival();
  // In sequence, not together: on a first visit each request without a cookie
  // would mint its own anonymous account, and the browser keeps only one.
  await refreshAccount();
  refreshJobs();
})();
