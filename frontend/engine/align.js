/* The alignment engine, in the browser.
 *
 * A transcript with times and a book without them go in; subtitles worded by
 * the book and timed by the transcript come out.
 *
 * This is a port of SubPlz's aligner (ats.align, subplz.align.shift_align),
 * kept statement-for-statement where it is a port, quirks included: the tests
 * in tests/engine run it against the reference implementation's real output,
 * stage by stage, and it is only trustworthy while it reproduces that exactly.
 *
 * What is not a port is anchoredAlign. The reference aligns one chapter at a
 * time in a full dynamic-programming table and needs gigabytes to do it. This
 * aligns the whole book at once by anchoring on stretches unique to both texts
 * and solving exactly only between anchors - seconds and megabytes, which is
 * what makes doing it in a browser tab possible at all.
 *
 * Offsets are code points throughout, never UTF-16 units, so a slice cannot
 * split a surrogate pair however the text was cut.
 */

export const toCodePoints = (s) => Int32Array.from(s, (ch) => ch.codePointAt(0));

export function fromCodePoints(cps, from = 0, to = cps.length) {
  if (to <= from) return '';
  let out = '';
  for (let i = from; i < to; i += 8192) {
    out += String.fromCodePoint(...cps.subarray(i, Math.min(to, i + 8192)));
  }
  return out;
}

/* ------------------------------------------------------------------ language */

const JA = (() => {
  const map = new Map();
  // Katakana to hiragana: the recogniser picks between them at random.
  for (let i = 0; i < 0x56; i++) map.set(0x30a1 + i, 0x3041 + i);
  // Kanji numerals to digits. The digit string is shorter and wraps, which is
  // how both 十 and 拾 come out as １.
  const kansuu = toCodePoints('一二三四五六七八九十〇零壱弐参肆伍陸漆捌玖拾');
  const arabic = toCodePoints('１２３４５６７８９１００');
  kansuu.forEach((cp, i) => map.set(cp, arabic[i % arabic.length]));
  // ASCII to full width, so "A" in one text meets "Ａ" in the other.
  for (let cp = 0x21; cp < 0x7f; cp++) map.set(cp, cp + 0xfee0);
  // Particles written one way and pronounced another.
  for (const [a, b] of [['は', 'わ'], ['あ', 'わ'], ['お', 'を'], ['へ', 'え']]) {
    map.set(a.codePointAt(0), b.codePointAt(0));
  }
  return map;
})();

// Everything that is not a letter or digit goes, except 。 when it leads a run.
const JA_NOISE = /(?![。])[\p{C}\p{M}\p{P}\p{S}\p{Z}\sー々ゝ]+/gu;
// Collapse repeats: long vowels and stutters are where the two texts disagree.
const JA_REPEATS = /(.)(?=\1+)/gu;

/** Only Japanese has rules of its own; everything else is case-folded. */
export function language(code) {
  const japanese = code === 'ja';
  const translate = (s) => {
    if (!japanese) return s.toLowerCase();
    let out = '';
    for (const ch of s) {
      const cp = ch.codePointAt(0);
      out += JA.has(cp) ? String.fromCodePoint(JA.get(cp)) : ch;
    }
    return out.toLowerCase();
  };
  const clean = japanese
    ? (s) => translate(s).replace(JA_NOISE, '').replace(JA_REPEATS, '')
    : translate;
  return { code, translate, clean };
}

/* --------------------------------------------------------------------- gotoh */

// The reference's scores, times ten so ties are exact.
const MATCH = 10, MISMATCH = -6, OPEN = -8, EXTEND = -5;
const NEG = -(2 ** 29);

export const cells = (n, m) => (n + 1) * (m + 1);

/** Exact global alignment with affine gaps. Returns {score, t, q}: a path of breakpoints. */
export function gotoh(target, query) {
  const n = target.length, m = query.length, width = m + 1;
  const trace = new Uint8Array(cells(n, m));
  let pm = new Int32Array(width).fill(NEG), px = new Int32Array(width).fill(NEG),
      py = new Int32Array(width).fill(NEG);
  let cm = new Int32Array(width), cx = new Int32Array(width), cy = new Int32Array(width);

  pm[0] = 0;
  for (let j = 1; j <= m; j++) { py[j] = OPEN + (j - 1) * EXTEND; trace[j] = j === 1 ? 0 : 32; }

  for (let i = 1; i <= n; i++) {
    const row = i * width, t = target[i - 1];
    cm[0] = NEG; cy[0] = NEG; cx[0] = OPEN + (i - 1) * EXTEND;
    trace[row] = i === 1 ? 0 : 4;
    for (let j = 1; j <= m; j++) {
      let bits = 0, best = pm[j - 1];
      if (px[j - 1] > best) { best = px[j - 1]; bits = 1; }
      if (py[j - 1] > best) { best = py[j - 1]; bits = 2; }
      cm[j] = best + (t === query[j - 1] ? MATCH : MISMATCH);

      let bx = pm[j] + OPEN, xb = 0;
      if (px[j] + EXTEND > bx) { bx = px[j] + EXTEND; xb = 4; }
      if (py[j] + OPEN > bx) { bx = py[j] + OPEN; xb = 8; }
      cx[j] = bx;

      let by = cm[j - 1] + OPEN, yb = 0;
      if (cx[j - 1] + OPEN > by) { by = cx[j - 1] + OPEN; yb = 16; }
      if (cy[j - 1] + EXTEND > by) { by = cy[j - 1] + EXTEND; yb = 32; }
      cy[j] = by;

      trace[row + j] = bits | xb | yb;
    }
    [pm, cm] = [cm, pm]; [px, cx] = [cx, px]; [py, cy] = [cy, py];
  }

  let state = 0, score = pm[m];
  if (px[m] > score) { score = px[m]; state = 1; }
  if (py[m] > score) { score = py[m]; state = 2; }

  const ts = [n], qs = [m];
  let i = n, j = m, last = -1;
  while (i > 0 || j > 0) {
    if (last !== -1 && last !== state) { ts.push(i); qs.push(j); }
    last = state;
    const bits = trace[i * width + j];
    if (state === 0) { state = bits & 3; i--; j--; }
    else if (state === 1) { state = (bits >> 2) & 3; i--; }
    else { state = (bits >> 4) & 3; j--; }
  }
  if (ts[ts.length - 1] !== 0 || qs[qs.length - 1] !== 0) { ts.push(0); qs.push(0); }
  return { score, t: ts.reverse(), q: qs.reverse() };
}

export function pathScore(target, query, path) {
  let total = 0;
  for (let k = 1; k < path.t.length; k++) {
    const dt = path.t[k] - path.t[k - 1], dq = path.q[k] - path.q[k - 1];
    if (dt > 0 && dq > 0) {
      for (let d = 0; d < dt; d++) {
        total += target[path.t[k - 1] + d] === query[path.q[k - 1] + d] ? MATCH : MISMATCH;
      }
    } else if (dt > 0 || dq > 0) total += OPEN + (Math.max(dt, dq) - 1) * EXTEND;
  }
  return total;
}

/* ------------------------------------------------------------------ anchored */

// FNV-1a over code points, folded to 53 bits so it is an exact JS number.
function gramHash(seq, at, k) {
  let lo = 0x811c9dc5, hi = 0x1000193;
  for (let d = 0; d < k; d++) {
    const c = seq[at + d];
    lo = Math.imul(lo ^ c, 0x01000193) >>> 0;
    hi = Math.imul(hi ^ (c >>> 3), 0x27d4eb2f) >>> 0;
  }
  return (hi & 0x1fffff) * 4294967296 + lo;
}

/** k-gram -> its position, or -2 when it occurs more than once. */
function uniqueGrams(seq, from, to, k) {
  const map = new Map();
  for (let i = from; i <= to - k; i++) {
    const h = gramHash(seq, i, k);
    map.set(h, map.has(h) ? -2 : i);
  }
  return map;
}

function anchors(target, query, t0, t1, q0, q1, k) {
  if (t1 - t0 < k || q1 - q0 < k) return [];
  const inTarget = uniqueGrams(target, t0, t1, k);
  const inQuery = uniqueGrams(query, q0, q1, k);
  const runs = [];
  let current = null;
  for (let qi = q0; qi <= q1 - k; qi++) {
    const h = gramHash(query, qi, k);
    const ti = inQuery.get(h) === qi ? inTarget.get(h) : undefined;
    let same = ti !== undefined && ti >= 0;
    for (let d = 0; same && d < k; d++) same = target[ti + d] === query[qi + d];
    if (!same) { current = null; continue; }
    if (current && ti === current.t + (qi - current.q)) current.len = qi - current.q + k;
    else runs.push(current = { t: ti, q: qi, len: k });
  }
  return runs;
}

/** Longest chain of runs in order on both sides (patience sort), overlaps trimmed. */
function chainOf(runs) {
  if (!runs.length) return runs;
  const byT = runs.slice().sort((a, b) => a.t - b.t);
  const tails = [], prev = new Int32Array(byT.length).fill(-1);
  byT.forEach((r, i) => {
    let lo = 0, hi = tails.length;
    while (lo < hi) { const mid = (lo + hi) >>> 1; if (byT[tails[mid]].q < r.q) lo = mid + 1; else hi = mid; }
    if (lo > 0) prev[i] = tails[lo - 1];
    tails[lo] = i;
  });
  const picked = [];
  for (let at = tails[tails.length - 1]; at >= 0; at = prev[at]) picked.push(byT[at]);
  picked.reverse();

  const out = [];
  let endT = 0, endQ = 0;
  for (const r of picked) {
    const cut = Math.max(endT - r.t, endQ - r.q, 0);
    if (cut >= r.len) continue;
    r.t += cut; r.q += cut; r.len -= cut;
    out.push(r);
    endT = r.t + r.len; endQ = r.q + r.len;
  }
  return out;
}

/** Global alignment of inputs too big for one table. Same shape of result as gotoh. */
export function anchoredAlign(target, query, { exactCells = 4_000_000, firstK = 14, lastK = 5 } = {}) {
  const ts = [0], qs = [0];
  let lastKind = 0;
  const moveTo = (t, q) => {
    const pt = ts[ts.length - 1], pq = qs[qs.length - 1];
    if (t === pt && q === pq) return;
    const dt = t - pt, dq = q - pq;
    if (dt > 0 && dq > 0 && dt !== dq) {   // a diagonal and a gap in one step
      const d = Math.min(dt, dq);
      moveTo(pt + d, pq + d); moveTo(t, q);
      return;
    }
    const kind = dt > 0 && dq > 0 ? 1 : dt > 0 ? 2 : 3;
    if (kind === lastKind && ts.length > 1) { ts[ts.length - 1] = t; qs[qs.length - 1] = q; }
    else { ts.push(t); qs.push(q); }
    lastKind = kind;
  };

  // An explicit stack: a book is thousands of ranges deep in places.
  const work = [[0, target.length, 0, query.length, firstK]];
  while (work.length) {
    const job = work.pop();
    if (job.length === 2) { moveTo(job[0], job[1]); continue; }
    const [t0, t1, q0, q1, k] = job;
    const n = t1 - t0, m = q1 - q0;
    if (n === 0 || m === 0) { moveTo(t1, q1); continue; }
    if (cells(n, m) <= exactCells) {
      const sub = gotoh(target.subarray(t0, t1), query.subarray(q0, q1));
      for (let i = 0; i < sub.t.length; i++) moveTo(t0 + sub.t[i], q0 + sub.q[i]);
      continue;
    }

    let kk = k, chain = [];
    for (; kk >= lastK; kk -= 3) {
      chain = chainOf(anchors(target, query, t0, t1, q0, q1, kk));
      if (chain.length) break;
    }
    const next = [];
    if (!chain.length) {
      // Two long stretches with nothing in common: halve both and carry on.
      const tm = t0 + (n >> 1), qm = q0 + (m >> 1);
      next.push([t0, tm, q0, qm, lastK], [tm, t1, qm, q1, lastK]);
    } else {
      let ct = t0, cq = q0;
      for (const r of chain) {
        next.push([ct, r.t, cq, r.q, kk], [r.t + r.len, r.q + r.len]);
        ct = r.t + r.len; cq = r.q + r.len;
      }
      next.push([ct, t1, cq, q1, kk]);
    }
    for (let i = next.length - 1; i >= 0; i--) work.push(next[i]);
  }
  moveTo(target.length, query.length);
  const path = { t: ts, q: qs };
  path.score = pathScore(target, query, path);
  return path;
}

/* ----------------------------------------------------------------- align_sub */

const FULL_STOP = '。'.codePointAt(0);

function distinctExceptFullStop(cps, start, end) {
  const seen = new Set();
  for (let k = start; k < Math.min(end, cps.length); k++) if (cps[k] !== FULL_STOP) seen.add(cps[k]);
  return seen.size;
}

/** Which piece of which paragraph each transcript segment covers. Spans are [start, end, sub]. */
export function alignSub(path, text, subs, thing = 2) {
  let line = 0, sub = 0, toff = 0, off = 0;
  const pos = [0, 0], p = [0, 0], gaps = [0, 0];
  const segments = [[]];
  const last = () => segments[segments.length - 1];

  for (let i = 0; i < path.t.length; i++) {
    const c0 = path.t[i], c1 = path.q[i];
    const isGap = c0 - p[0] === 0 || c1 - p[1] === 0;

    while (sub < subs.length && pos[1] + subs[sub].length <= c1) {
      if (line >= text.length) return segments.slice(0, text.length);
      const subLen = subs[sub].length;
      pos[1] += subLen;
      if (isGap) { gaps[0] += Math.max(pos[1] - p[1], 0); gaps[1] += Math.max(pos[0] - p[0], 0); }

      const diff = subLen + gaps[1] - gaps[0];
      if (diff > Math.floor(subLen / 4)) {
        let target = toff + diff + off;
        off = 0;
        while (line < text.length && target >= text[line].length) {
          const start = toff, end = text[line].length;
          if (end - start !== 0) {
            const prev = last();
            if (end - start < thing || distinctExceptFullStop(text[line], start, end) < thing) {
              if (prev.length) prev[prev.length - 1][1] = end;
              else prev.push([start, end, sub]);
            } else prev.push([start, end, sub]);
          }
          segments.push([]);
          pos[0] += end - start;
          target -= text[line].length;
          line++;
          toff = 0;
        }
        pos[0] += target - toff;
        last().push([toff, target, sub]);
        toff = target;
      } else {
        const prev = last();
        if (toff >= Math.floor(text[line].length / 2) && prev.length) {
          prev[prev.length - 1][1] += diff;
          toff += diff;
        } else off += diff;
      }

      sub++;
      gaps[0] = 0; gaps[1] = 0;
      p[0] = Math.max(pos[0], p[0]); p[1] = Math.max(pos[1], p[1]);
    }

    if (isGap) { gaps[0] += c1 - p[1]; gaps[1] += c0 - p[0]; }
    p[0] = c0; p[1] = c1;
  }
  return segments.slice(0, text.length);
}

/** Spans were computed on cleaned text; map them back onto the original. */
export function fix(lang, original, edited, segments) {
  segments.forEach((spans, l) => {
    const o = toCodePoints(lang.translate(original[l])), e = edited[l];
    const m = new Int32Array(e.length + 1).fill(-1);
    let ei = 0;
    for (let oi = 0; oi < o.length; oi++) {
      if (ei < e.length && o[oi] === e[ei]) { m[ei] = oi; ei++; }
    }
    m[ei] = o.length;
    m[0] = 0;
    let lastSet = 0;
    for (let i = 0; i < e.length; i++) { if (m[i] !== -1) lastSet = i; else m[i] = m[lastSet]; }
    if (m[e.length] === -1) m[e.length] = o.length;
    const clamp = (v) => Math.min(Math.max(v, 0), e.length);
    for (const f of spans) { f[0] = m[clamp(f[0])]; f[1] = m[clamp(f[1])]; }
  });
}

/** Python indexing: negatives count from the end; out of range is "no character". */
const at = (t, i) => { const k = i < 0 ? i + t.length : i; return k >= 0 && k < t.length ? t[k] : -1; };

/** Nudge span ends so punctuation stays with the words it belongs to. */
export function fixPunc(text, segments, prepend, append, nopend) {
  segments.forEach((s, l) => {
    if (!s.length) return;
    const t = toCodePoints(text[l]);
    for (let k = 0; k < s.length; k++) {
      const p = s[k], f = k + 1 < s.length ? s[k + 1] : s[k];
      const connected = f[0] === p[1];
      for (let loop = 0; loop <= 20; loop++) {
        if (p[1] < t.length && append.has(at(t, p[1]))) p[1] += 1;
        else if (prepend.has(at(t, p[1] - 1))) p[1] -= 1;
        else if ((p[1] > 0 && nopend.has(at(t, p[1] - 1))) ||
                 (p[1] < t.length && nopend.has(at(t, p[1]))) ||
                 (p[1] < t.length - 1 && nopend.has(at(t, p[1] + 1)))) {
          let start = p[1] - 1, end = p[1];
          if (p[1] < t.length - 1) {
            const here = at(t, p[1]);
            if ((nopend.has(at(t, p[1] + 1)) && 0x4e00 > here) || here > 0x9faf) end += 1;
          }
          while (start > 0 && nopend.has(at(t, start))) start -= 1;
          while (end < t.length - 1 && nopend.has(at(t, end))) end += 1;

          if (prepend.has(at(t, start))) { if (p[1] === start) break; p[1] = start; }
          else if (append.has(at(t, start))) { if (p[1] === start + 1) break; p[1] = start + 1; }
          else if (end < t.length && prepend.has(at(t, end))) { if (p[1] === end) break; p[1] = end; }
          else if (end < t.length && append.has(at(t, end))) { if (p[1] === end + 1) break; p[1] = end + 1; }
          else break;
        } else break;
      }
      if (connected) f[0] = p[1];
    }
  });
}

/* ---------------------------------------------------------------------- subs */

export const START_PUNC = '『「(（《｟[{"\'“¿' + '\'“"¿([{-『「（〈《〔【｛［｟＜<‘“〝※';
export const END_PUNC = '\'"・.。!！?？:：”>＞⦆)]}』」）〉》〕】｝］’〟／＼～〜~;；─―–-➡';
export const OTHER_PUNC = '＊　,，、…';
export const NOPEND = 'うぁぃぅぇぉっゃゅょゎゕゖァィゥェォヵㇰヶㇱㇲッㇳㇴㇵㇶㇷㇷ゚ㇸㇹㇺャュョㇻㇼㇽㇾㇿヮ…　 ';
export const UNMATCHED = '＊';

const cpSet = (s) => new Set(toCodePoints(s));
export const PREPEND_SET = cpSet(START_PUNC);
export const APPEND_SET = cpSet(END_PUNC + OTHER_PUNC);
export const NOPEND_SET = cpSet(NOPEND);

const PUNCT = new Set(START_PUNC + END_PUNC + OTHER_PUNC);
const START = new Set(START_PUNC), END = new Set(END_PUNC);

function pySlice(cps, a, b) {
  const n = cps.length, fixIndex = (v) => Math.min(Math.max(v < 0 ? v + n : v, 0), n);
  return fromCodePoints(cps, fixIndex(a), fixIndex(b));
}

/** One cue per transcript segment: timed by the recogniser, worded by the book. */
export function toSubs(text, subs, alignment, offset = 0) {
  const flat = [];
  alignment.forEach((spans, line) => spans.forEach((span) => flat.push({ span, line })));
  const lines = text.map(toCodePoints);
  const out = [];
  let start = 0, end = 0;
  subs.forEach((s, si) => {
    while (end < flat.length && flat[end].span[2] === si) end++;
    let body = '';
    for (let k = start; k < end; k++) body += pySlice(lines[flat[k].line], flat[k].span[0], flat[k].span[1]);
    out.push({ text: body.trim() ? body : UNMATCHED + s.text, start: s.start + offset, end: s.end + offset });
    start = end;
  });
  return out;
}

const punctIndices = (s) => { const r = []; for (let i = 0; i < s.length; i++) if (PUNCT.has(s[i])) r.push(i); return r; };
const countNonPunct = (s) => { let n = 0; for (let i = 0; i < s.length; i++) if (!PUNCT.has(s[i])) n++; return n; };
const doubleComma = (a, b) => a[a.length - 1] === '、' && b[b.length - 1] === '、';

/** Move a stranded character or two across a cue boundary. Cues are mutated, as in the original. */
export function shiftAlign(segments) {
  const fresh = [];
  let startIndex = 0;   // read by the second pass too, as in the original
  segments.forEach((segment, i) => {
    let text = segment.text;
    const idx = punctIndices(text);
    if (!idx.length) { fresh.push(segment); return; }
    startIndex = idx[0];
    const nonPunc = countNonPunct(text.slice(0, startIndex));
    if (nonPunc === 0 || countNonPunct(text.slice(startIndex + 1)) === 0) { fresh.push(segment); return; }
    const prev = fresh[fresh.length - 1];
    if (nonPunc <= 2 && i > 0 && fresh.length && !END.has(prev.text[prev.text.length - 1]) &&
        !doubleComma(prev.text, text.slice(0, startIndex + 1))) {
      prev.text += text.slice(0, startIndex + 1);
      text = text.slice(startIndex + 1);
    }
    fresh.push({ text, start: segment.start, end: segment.end });
  });

  const final = [];
  for (let i = 0; i < fresh.length; i++) {
    const segment = fresh[i];
    let text = segment.text;
    const idx = punctIndices(text);
    if (idx.length) {
      const lastIndex = idx[idx.length - 1], tail = text.slice(lastIndex);
      if (countNonPunct(tail) === 0 || tail.length === text.length) { final.push(segment); continue; }
      if (countNonPunct(tail) <= 2 && i + 1 < fresh.length && !END.has(fresh[i + 1].text[0]) &&
          !doubleComma(fresh[i + 1].text.slice(0, 1), text.slice(0, startIndex + 1))) {
        // The original reaches into the *input* list here. Kept: the fixtures depend on it.
        const next = segments[i + 1];
        next.text = text.slice(lastIndex + 1) + next.text;
        text = text.slice(0, lastIndex + 1);
        final.push({ text, start: segment.start, end: segment.end });
        fresh[i + 1] = next;
        continue;
      }
    }
    final.push({ text, start: segment.start, end: segment.end });
  }

  if (final.length >= 2) {
    const stray = /(」「(.{1,2})、$|」「(.{1})、$)/u;
    const adjusted = [];
    final.forEach((segment, i) => {
      const m = stray.exec(segment.text);
      if (m && i < final.length - 1) {
        final[i + 1].text = m[0] + final[i + 1].text;
        segment.text = segment.text.slice(0, m.index);
      }
      if (segment.text && END.has(segment.text[0]) && i > 0) {
        adjusted[adjusted.length - 1].text += segment.text[0];
        segment.text = segment.text.slice(1);
      }
      if (segment.text && START.has(segment.text[segment.text.length - 1]) && i < final.length - 1) {
        final[i + 1].text = segment.text[segment.text.length - 1] + final[i + 1].text;
        segment.text = segment.text.slice(0, -1);
      }
      adjusted.push(segment);
    });
  }
  return final.map((c) => ({ text: c.text.trim(), start: c.start, end: c.end }));
}

/* ------------------------------------------------------------------ pipeline */

const EXACT_CELL_LIMIT = 64_000_000;

function concat(parts) {
  const out = new Int32Array(parts.reduce((n, p) => n + p.length, 0));
  let o = 0;
  for (const p of parts) { out.set(p, o); o += p.length; }
  return out;
}

export function align(transcript, paragraphs, lang, { anchoredOnly = false, exactCells } = {}) {
  if (!transcript.length) return [];
  const subsClean = transcript.map((s) => toCodePoints(lang.clean(s.text)));
  const textClean = paragraphs.map((p) => toCodePoints(lang.clean(p)));
  const query = concat(subsClean), target = concat(textClean);
  if (!target.length || !query.length) {
    return transcript.map((s) => ({ text: UNMATCHED + s.text, start: s.start, end: s.end }));
  }
  const path = !anchoredOnly && cells(target.length, query.length) <= EXACT_CELL_LIMIT
    ? gotoh(target, query) : anchoredAlign(target, query, exactCells ? { exactCells } : {});
  const spans = alignSub(path, textClean, subsClean);
  fix(lang, paragraphs, textClean, spans);
  fixPunc(paragraphs, spans, PREPEND_SET, APPEND_SET, NOPEND_SET);
  return shiftAlign(toSubs(paragraphs, transcript, spans));
}

// A paragraph counts as heard when this share of it agrees with the transcript...
const HEARD = 0.20;
// ...and unheard ones are dropped only in runs this long: one mangled heading
// was still read aloud; a page of nothing was not.
const MIN_UNREAD_RUN = 240;

/** The paragraphs that were actually narrated: front matter, notes and the like removed. */
export function narratedOnly(transcript, paragraphs, lang) {
  const textClean = paragraphs.map((p) => toCodePoints(lang.clean(p)));
  const target = concat(textClean);
  const query = concat(transcript.map((s) => toCodePoints(lang.clean(s.text))));
  if (!target.length || !query.length) return paragraphs;

  const path = anchoredAlign(target, query);
  const agreed = new Uint8Array(target.length);
  for (let k = 1; k < path.t.length; k++) {
    const t0 = path.t[k - 1], q0 = path.q[k - 1], dt = path.t[k] - t0;
    if (dt > 0 && path.q[k] - q0 > 0) {
      for (let d = 0; d < dt; d++) agreed[t0 + d] = target[t0 + d] === query[q0 + d] ? 1 : 0;
    }
  }

  let o = 0;
  const heard = textClean.map((cps) => {
    let hits = 0;
    for (let i = o; i < o + cps.length; i++) hits += agreed[i];
    o += cps.length;
    return cps.length === 0 || hits >= HEARD * cps.length;
  });

  const keep = paragraphs.map(() => true);
  for (let i = 0; i < paragraphs.length;) {
    if (heard[i]) { i++; continue; }
    let j = i, chars = 0;
    while (j < paragraphs.length && (!heard[j] || textClean[j].length === 0)) { chars += textClean[j].length; j++; }
    if (chars >= MIN_UNREAD_RUN) for (let k = i; k < j; k++) keep[k] = false;
    i = j;
  }
  const out = paragraphs.filter((_, k) => keep[k]);
  return out.length ? out : paragraphs;
}

/** A whole book at once. No chapter matching: the alignment itself says what was read. */
export function alignBook(transcript, paragraphs, lang) {
  const narrated = narratedOnly(transcript, paragraphs, lang);
  const cues = align(transcript, narrated, lang);
  const matched = cues.filter((c) => !c.text.startsWith(UNMATCHED)).length;
  return {
    cues,
    paragraphsUsed: narrated.length,
    paragraphsDropped: paragraphs.length - narrated.length,
    matchRate: cues.length ? matched / cues.length : 0,
  };
}

/* ----------------------------------------------------------------------- srt */

export function stamp(seconds) {
  const ms = Math.round(Math.max(seconds, 0) * 1000);
  const pad = (v, n = 2) => String(v).padStart(n, '0');
  return `${pad(Math.floor(ms / 3600000))}:${pad(Math.floor(ms / 60000) % 60)}:${pad(Math.floor(ms / 1000) % 60)},${pad(ms % 1000, 3)}`;
}

/** One line of text per cue, always: Hoshi Reader reads exactly the third line of each block. */
export function writeSrt(cues) {
  let out = '', n = 0;
  for (const cue of cues) {
    const text = cue.text.replace(/\s*[\r\n]+\s*/g, ' ').trim();
    if (!text) continue;
    out += `${++n}\n${stamp(cue.start)} --> ${stamp(cue.end)}\n${text}\n\n`;
  }
  return out;
}
