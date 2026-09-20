/* SubPlz web UI. No framework, no build step - one file, served as-is. */

const $ = (id) => document.getElementById(id);

const el = {
  dropzone: $('dropzone'), picker: $('filepicker'), browse: $('browse'),
  uploading: $('uploading'), upbar: $('upbar'), uptext: $('uptext'),
  error: $('error'),
  confirm: $('confirm'), cAudio: $('c-audio'), cText: $('c-text'),
  language: $('language'), detected: $('detected'), splitnote: $('splitnote'),
  start: $('start'), discard: $('discard'), eta: $('eta'),
  jobsSection: $('jobs-section'), jobs: $('jobs'), quota: $('quota'),
  freetier: $('freetier'), staged: $('staged'), dzTitle: $('dz-title'),
  match: $('match'), matchBadge: $('match-badge'),
  matchSummary: $('match-summary'), matchWarnings: $('match-warnings'),
};

let languages = [];
let languagesReady = null;  // resolves once the <select> is populated
let draft = null;           // the job awaiting confirmation
let pollTimer = null;

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
    throw new Error(detail);
  }
  return res.status === 204 ? null : res.json();
}

/* ---------------- setup ---------------- */

async function loadLanguages() {
  languages = await api('/api/languages');
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
  updateSplitNote();
}

async function refreshQuota() {
  try {
    const a = await api('/api/account');
    if (a.free_tier_summary && el.freetier) {
      el.freetier.textContent = a.free_tier_summary.replace(/^./, (c) => c.toUpperCase()) + '.';
    }
    if (!a.billing_enabled) {
      el.quota.hidden = true;
      return;
    }
    el.quota.hidden = false;
    el.quota.classList.toggle('blocked', !a.allowed);
    el.quota.textContent = a.allowed
      ? `${a.remaining} conversion${a.remaining === 1 ? '' : 's'} left`
      : 'Free conversion used — add credit to continue';
  } catch { /* quota is cosmetic; never block the UI on it */ }
}

/* ---------------- upload ---------------- */

/* Files can arrive one drop at a time, so collect them until both halves are
   here, then upload. Ignores the cover art and readme files that ride along
   inside audiobook folders. */
const AUDIO_RE = /\.(m4b|m4a|mp3|opus|ogg|oga|flac|wav|aac|wma|mka|mkv|mp4|webm|avi|mov)$/i;
const TEXT_RE = /(\.fb2\.zip|\.(epub|txt|srt|vtt|ass|fb2|mobi|azw|azw3|prc))$/i;

const staged = { audio: [], text: null };

function addFiles(files) {
  clearError();
  const list = [...files];
  const audio = list.filter((f) => AUDIO_RE.test(f.name));
  const text = list.filter((f) => TEXT_RE.test(f.name));

  if (!audio.length && !text.length) {
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
  if (staged.audio.length && staged.text) {
    uploadFiles([...staged.audio, staged.text]);
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
  renderStaged();
}

document.querySelectorAll('.slot-x').forEach((btn) => {
  btn.addEventListener('click', () => {
    if (btn.dataset.slot === 'audio') staged.audio = [];
    else staged.text = null;
    renderStaged();
  });
});

function uploadFiles(files) {
  clearError();
  const list = [...files];

  el.dropzone.hidden = true;
  el.staged.hidden = true;
  el.confirm.hidden = true;
  el.uploading.hidden = false;
  el.upbar.style.width = '0%';
  el.uptext.textContent = 'Starting…';

  const form = new FormData();
  for (const f of list) form.append('files', f, f.name);

  // XHR rather than fetch: it reports upload progress, and these files are big.
  const xhr = new XMLHttpRequest();
  xhr.open('POST', '/api/uploads');
  xhr.withCredentials = true;

  xhr.upload.onprogress = (e) => {
    if (!e.lengthComputable) return;
    const pct = Math.round((e.loaded / e.total) * 100);
    el.upbar.style.width = `${pct}%`;
    el.uptext.textContent = pct < 100
      ? `${pct}% — ${fmtBytes(e.loaded)} of ${fmtBytes(e.total)}`
      : 'Analysing the book…';
  };

  xhr.onload = () => {
    el.uploading.hidden = true;
    if (xhr.status >= 200 && xhr.status < 300) {
      try { showConfirm(JSON.parse(xhr.responseText)); }
      catch { showError('Server sent an unreadable response.'); resetToDrop(); }
    } else {
      let msg = `Upload failed (${xhr.status}).`;
      try {
        const b = JSON.parse(xhr.responseText);
        if (b.detail) msg = typeof b.detail === 'string' ? b.detail : msg;
      } catch { /* keep the generic message */ }
      showError(msg);
      resetToDrop();
    }
  };

  xhr.onerror = () => {
    el.uploading.hidden = true;
    showError('Upload failed: lost connection to the server.');
    resetToDrop();
  };

  xhr.send(form);
}

function resetToDrop() {
  draft = null;
  clearStaged();
  el.dropzone.hidden = false;
  el.confirm.hidden = true;
  el.uploading.hidden = true;
  el.picker.value = '';
}

/* ---------------- confirm ---------------- */

async function showConfirm(payload) {
  // The options must exist before we can select the detected language.
  try { await languagesReady; } catch { /* handled at boot */ }

  draft = payload.job;
  const d = payload.detected;

  const dur = fmtDuration(draft.audio_duration_seconds);
  const bits = [
    draft.audio_parts > 1 ? `${draft.audio_parts} parts` : null,
    fmtBytes(draft.audio_bytes),
    dur,
  ].filter(Boolean);
  el.cAudio.innerHTML =
    `${escapeHtml(draft.audio_filename)} <span class="meta">${bits.join(' · ')}</span>`;
  el.cText.textContent = draft.text_filename;

  setLanguage(draft.language);

  if (d.code && d.supported) {
    const pct = Math.round(d.confidence * 100);
    el.detected.textContent = `detected ${d.name} · ${pct}%`;
    el.detected.classList.toggle('low', d.confidence < 0.7);
    el.detected.hidden = false;
  } else if (d.code) {
    el.detected.textContent = `detected ${d.name} — not supported, pick one`;
    el.detected.classList.add('low');
    el.detected.hidden = false;
  } else {
    el.detected.hidden = true;
  }

  renderMatch(payload.match);

  el.eta.textContent = draft.audio_duration_seconds
    ? `Alignment usually takes a fraction of the book's length, but on CPU it can approach it. ${dur} of audio — expect a long run.`
    : '';

  el.confirm.hidden = false;
  el.start.disabled = false;
  el.confirm.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}

/* The preflight score, using the backend's own matching rule. A poor score is
   shown but never blocks: the check samples the audio, so it can be wrong, and
   it is the user's book. */
function renderMatch(m) {
  if (!m || m.skipped || m.verdict === 'unknown') {
    el.match.hidden = true;
    el.start.textContent = 'Start alignment';
    return;
  }

  el.match.hidden = false;
  el.match.className = `match ${m.verdict}`;
  el.matchBadge.textContent =
    { good: 'match', marginal: 'weak match', poor: 'no match' }[m.verdict] || m.verdict;
  el.matchSummary.textContent = m.summary;

  el.matchWarnings.innerHTML = '';
  for (const w of m.warnings || []) {
    const li = document.createElement('li');
    li.textContent = w;
    el.matchWarnings.appendChild(li);
  }

  // Make the user's choice explicit when we expect this to fail.
  el.start.textContent =
    m.verdict === 'poor' ? 'Start anyway' : 'Start alignment';
}

function updateSplitNote() {
  // The note comes from the server, so the UI carries no knowledge of which
  // alignment backend is running or how it splits sentences.
  const lang = languages.find((l) => l.code === el.language.value);
  el.splitnote.textContent = (lang && lang.note) || '';
}

el.language.addEventListener('change', updateSplitNote);

el.discard.addEventListener('click', async () => {
  if (draft) { try { await api(`/api/jobs/${draft.id}`, { method: 'DELETE' }); } catch {} }
  clearError();
  resetToDrop();
  refreshJobs();
});

el.start.addEventListener('click', async () => {
  if (!draft) return;
  el.start.disabled = true;
  clearError();
  try {
    await api(`/api/jobs/${draft.id}/start`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ language: el.language.value }),
    });
    resetToDrop();
    refreshJobs();
    refreshQuota();
  } catch (e) {
    showError(e.message);
    el.start.disabled = false;
  }
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

  const active = j.status === 'running' || j.status === 'queued';
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
      refreshJobs(); refreshQuota();
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
  const visible = jobs.filter((j) => j.status !== 'draft');
  el.jobsSection.hidden = visible.length === 0;
  el.jobs.innerHTML = '';
  for (const j of visible) el.jobs.appendChild(jobCard(j));

  const anyActive = visible.some((j) => j.status === 'running' || j.status === 'queued');
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

/* ---------------- boot ---------------- */

(async function init() {
  languagesReady = loadLanguages();
  try { await languagesReady; }
  catch (e) { showError(`Could not reach the server: ${e.message}`); }
  refreshQuota();
  refreshJobs();
})();
