const express = require('express');
const path = require('path');
const store = require('./lib/manualStore');

const app = express();
app.set('trust proxy', true);
const PORT = process.env.PORT || 8080;
const FEED_KEY = 'primary-2026';
const LABEL = '2026 Minnesota State Primary';
const SOURCE_LABEL = 'Patch staff (manual entry from official sources)';
const ADMIN_USER = process.env.ADMIN_USER || 'patch';
const ADMIN_PASSWORD = process.env.ADMIN_PASSWORD || null;

app.use(express.static(path.join(__dirname, 'public')));
app.use(express.urlencoded({ extended: false }));

function escapeHtml(str) {
  return String(str ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

function partyColor(party) {
  if (/republican/i.test(party)) return '#004C05';
  if (/dfl|democrat/i.test(party)) return '#004C05';
  return '#009E13';
}

function fmtVotes(v) {
  return v === null || v === undefined ? '—' : Number(v).toLocaleString('en-US');
}

function fmtPct(p) {
  return p === null || p === undefined ? '—' : `${p.toFixed(2)}%`;
}

// --- Admin auth -------------------------------------------------------
function requireAdmin(req, res, next) {
  if (!ADMIN_PASSWORD) {
    res.status(503).send('Admin panel disabled: set the ADMIN_PASSWORD env var to enable /admin.');
    return;
  }
  const header = req.headers.authorization || '';
  const [scheme, encoded] = header.split(' ');
  if (scheme === 'Basic' && encoded) {
    const [user, pass] = Buffer.from(encoded, 'base64').toString('utf8').split(':');
    if (user === ADMIN_USER && pass === ADMIN_PASSWORD) {
      next();
      return;
    }
  }
  res.set('WWW-Authenticate', 'Basic realm="MN Primary Admin"');
  res.status(401).send('Authentication required.');
}

// --- Public API ---------------------------------------------------------
function contestIndexPayload() {
  const contests = store.getMergedContests();
  return {
    label: LABEL,
    lastUpdated: store.lastUpdated(),
    fetchedAt: new Date().toISOString(),
    error: null,
    stale: false,
    manual: true,
    anyDataEntered: store.anyDataEntered(),
    contests: contests.map((c) => ({
      slug: c.slug,
      name: c.name,
      office: c.office,
      party: c.party,
      tag: c.tag,
      precinctsReporting: c.precinctsReporting,
      totalPrecincts: c.totalPrecincts,
      updatedAt: c.updatedAt,
    })),
  };
}

app.get('/api/mn/:feedKey/index', (req, res) => {
  res.json(contestIndexPayload());
});

app.get('/api/mn/:feedKey/raw', (req, res) => {
  res.json({ ...contestIndexPayload(), contests: store.getMergedContests() });
});

app.get('/mn/:feedKey/race/:slug', (req, res) => {
  const contest = store.getContest(req.params.slug);

  if (!contest) {
    res.status(404).send(`<!DOCTYPE html><html><body style="font-family:Helvetica,Arial,sans-serif;color:#888;padding:20px;">Race not found: ${escapeHtml(req.params.slug)}</body></html>`);
    return;
  }

  const rows = contest.candidates.map((cand) => `
      <tr>
        <td class="name">${escapeHtml(cand.name)}</td>
        <td>${escapeHtml(cand.party)}</td>
        <td class="total">${fmtVotes(cand.votes)}</td>
        <td class="pct">${fmtPct(cand.pct)}</td>
      </tr>`).join('');

  const precinctLine = contest.totalPrecincts
    ? `${contest.precinctsReporting ?? 0} of ${contest.totalPrecincts} precincts reporting`
    : 'Precinct reporting: not yet entered';

  const updatedLine = contest.updatedAt
    ? new Date(contest.updatedAt).toLocaleString('en-US', { timeZone: 'America/Chicago' }) + ' CT'
    : 'not yet entered';

  const noDataBanner = !contest.updatedAt
    ? `<div class="stale-banner">⚠ No results entered yet for this race.</div>`
    : '';

  res.send(`<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>${escapeHtml(contest.name)} — Minnesota 2026 Primary</title>
<style>
  :root{ --accent:${partyColor(contest.party)}; --line:#e3e3e3; --ink:#1a1a1a; }
  *{box-sizing:border-box;}
  body{ margin:0; padding:14px 16px 20px; font-family:'Helvetica Neue',Arial,sans-serif; color:var(--ink); background:#fff; }
  .head{ border-bottom:2px solid var(--accent); padding-bottom:8px; margin-bottom:10px; }
  .label{ font-size:11px; letter-spacing:.1em; text-transform:uppercase; color:var(--accent); font-weight:700; }
  .sublabel{ font-size:11px; color:#888; margin-top:2px; }
  .stale-banner{
    background:#fff8e1; border:1px solid #f0c040; color:#7a5f00;
    font-size:11.5px; padding:6px 10px; border-radius:4px; margin-bottom:10px;
  }
  table{ width:100%; border-collapse:collapse; font-size:12.5px; margin-top:8px; }
  th{ text-align:left; font-size:10.5px; text-transform:uppercase; letter-spacing:.04em;
      color:#666; border-bottom:1px solid var(--line); padding:6px 8px; }
  td{ padding:6px 8px; border-bottom:1px solid #f0f0f0; }
  td.name{ font-weight:600; }
  td.total{ font-weight:700; color:var(--accent); }
  td.pct{ color:#444; }
  .table-foot{
    margin-top:10px; padding-top:8px; border-top:1px solid var(--line);
    font-size:11px; color:#777; line-height:1.7;
  }
  .table-foot .precinct-line{ font-weight:600; color:var(--ink); }
</style>
</head>
<body>

<div class="head">
  <div class="label">${escapeHtml(contest.name)}</div>
  <div class="sublabel">${SOURCE_LABEL} · Entered: ${escapeHtml(updatedLine)}</div>
</div>
${noDataBanner}
<table>
  <thead><tr><th>Candidate</th><th>Party</th><th>Votes</th><th>%</th></tr></thead>
  <tbody>${rows}</tbody>
</table>
<div class="table-foot">
  <div class="precinct-line">${escapeHtml(precinctLine)}</div>
  <div>Source: ${SOURCE_LABEL}</div>
  <div>Results are unofficial.</div>
</div>
<script>
  function sendHeight(){
    const h = document.body.scrollHeight;
    window.parent.postMessage({ iframeHeight: h, slug: "${escapeHtml(contest.slug)}" }, '*');
  }
  window.addEventListener('load', sendHeight);
  new ResizeObserver(sendHeight).observe(document.body);
</script>
</body>
</html>`);
});

// --- Status / debug / embeds ------------------------------------------
app.get('/status', (req, res) => {
  const contests = store.getMergedContests();
  const rows = contests.map((c) => `<tr><td>${escapeHtml(c.name)}</td>
    <td><code>${req.protocol}://${req.get('host')}/mn/${FEED_KEY}/race/${c.slug}</code></td>
    <td><a href="/mn/${FEED_KEY}/race/${c.slug}" target="_blank">preview ↗</a></td></tr>`).join('');

  res.send(`<!DOCTYPE html><html><head><meta charset="UTF-8">
<title>MN Manual Entry — Status</title>
<style>
  body{font-family:Helvetica,Arial,sans-serif;max-width:960px;margin:40px auto;color:#1a1a1a;padding:0 20px;}
  table{width:100%;border-collapse:collapse;margin-top:10px;}
  td,th{padding:8px 10px;border-bottom:1px solid #ddd;font-size:13px;text-align:left;}
  code{background:#f4f1ea;padding:2px 6px;border-radius:3px;font-size:11.5px;}
  pre{background:#f4f1ea;padding:16px;border-radius:4px;font-size:12px;overflow:auto;white-space:pre-wrap;}
  .note{background:#eaf2ff;border:1px solid #a9c6f5;color:#1a4fb3;padding:10px 14px;border-radius:4px;font-size:13px;}
</style>
</head><body>
  <h1>Minnesota 2026 Primary — Manual Entry Status</h1>
  <p class="note">This is the manual-entry standby tracker — results are typed in by Patch staff at <a href="/admin">/admin</a>, not pulled from a feed.</p>
  <p><a href="/debug">/debug</a> · <a href="/api/mn/${FEED_KEY}/raw">/api/mn/${FEED_KEY}/raw</a> · <a href="/">index</a> · <a href="/admin">/admin</a></p>
  <h2>${escapeHtml(LABEL)}</h2>
  <p>${contests.length} races · Last entry: ${escapeHtml(store.lastUpdated() || 'none yet')}</p>
  <table><thead><tr><th>Race</th><th>Embed URL</th><th></th></tr></thead><tbody>${rows}</tbody></table>
  <h2>Embed example</h2>
  <pre>&lt;iframe src="${req.protocol}://${req.get('host')}/mn/${FEED_KEY}/race/SLUG"
  width="600" height="600" frameborder="0" scrolling="auto" style="border:none;"&gt;
&lt;/iframe&gt;</pre>
</body></html>`);
});

app.get('/debug', (req, res) => {
  res.type('application/json').send(JSON.stringify({ ...contestIndexPayload(), contests: store.getMergedContests() }, null, 2));
});

app.get('/embeds.html', (req, res) => {
  const rows = store.getMergedContests().map((c) => {
    const src = `${req.protocol}://${req.get('host')}/mn/${FEED_KEY}/race/${c.slug}`;
    return `<div style="margin-bottom:28px;">
      <h3 style="font-family:Helvetica,Arial,sans-serif;">${escapeHtml(c.name)}</h3>
      <pre style="background:#f4f1ea;padding:14px;border-radius:4px;font-size:12px;overflow:auto;">&lt;iframe src="${escapeHtml(src)}"
  width="600" height="600" frameborder="0" scrolling="auto" style="border:none;"&gt;
&lt;/iframe&gt;</pre>
    </div>`;
  }).join('');
  res.send(`<!DOCTYPE html><html><head><meta charset="UTF-8"><title>Embed codes — MN 2026 Primary (manual)</title></head>
  <body style="max-width:800px;margin:40px auto;font-family:Helvetica,Arial,sans-serif;color:#1a1a1a;padding:0 20px;">
  <h1>Embed codes</h1>
  <p><a href="/">← Back to tracker</a></p>
  ${rows}
  </body></html>`);
});

// --- Admin: manual data entry -------------------------------------------
function renderAdminPage(req, savedSlug) {
  const contests = store.getMergedContests();
  const savedNotice = savedSlug
    ? `<div class="saved">✓ Saved "${escapeHtml(savedSlug)}" at ${escapeHtml(new Date().toLocaleTimeString())}</div>`
    : '';

  const sections = contests.map((c) => {
    const candRows = c.candidates.map((cand, i) => `
      <div class="cand-row">
        <label for="${c.slug}-cand-${i}">${escapeHtml(cand.name)} <span class="party">(${escapeHtml(cand.party)})</span></label>
        <input type="number" min="0" step="1" id="${c.slug}-cand-${i}" name="votes_${i}" value="${cand.votes ?? ''}" placeholder="votes">
      </div>`).join('');

    return `
    <form class="race-form" method="POST" action="/admin/save/${encodeURIComponent(c.slug)}">
      <h2>${escapeHtml(c.name)}</h2>
      <div class="precinct-row">
        <label>Precincts reporting
          <input type="number" min="0" step="1" name="precinctsReporting" value="${c.precinctsReporting ?? ''}">
        </label>
        <label>of total precincts
          <input type="number" min="0" step="1" name="totalPrecincts" value="${c.totalPrecincts ?? ''}">
        </label>
      </div>
      ${candRows}
      <button type="submit">Save ${escapeHtml(c.name)}</button>
      <span class="updated-at">${c.updatedAt ? 'Last saved: ' + escapeHtml(new Date(c.updatedAt).toLocaleString()) : 'Not yet entered'}</span>
    </form>`;
  }).join('<hr>');

  return `<!DOCTYPE html><html><head><meta charset="UTF-8">
<title>MN Primary — Manual Entry Admin</title>
<style>
  body{font-family:Helvetica,Arial,sans-serif;max-width:720px;margin:30px auto 80px;color:#1a1a1a;padding:0 20px;}
  h1{font-size:22px;}
  h2{font-size:16px;margin:0 0 10px;}
  .race-form{background:#fafafa;border:1px solid #ddd;border-radius:6px;padding:16px 18px;margin-bottom:20px;}
  .precinct-row{display:flex;gap:16px;margin-bottom:12px;font-size:13px;}
  .precinct-row input{width:80px;margin-left:6px;}
  .cand-row{display:flex;justify-content:space-between;align-items:center;padding:5px 0;font-size:13.5px;border-bottom:1px solid #eee;}
  .cand-row .party{color:#888;font-size:12px;}
  .cand-row input{width:110px;}
  button{margin-top:12px;padding:8px 16px;background:#004C05;color:#fff;border:none;border-radius:4px;font-size:13px;cursor:pointer;}
  button:hover{background:#009E13;}
  .updated-at{margin-left:12px;font-size:11.5px;color:#888;}
  .saved{background:#eaf7ec;border:1px solid #9fd8a6;color:#1a5c1a;padding:8px 14px;border-radius:4px;margin-bottom:16px;font-size:13px;}
</style>
</head><body>
  <h1>Minnesota 2026 Primary — Manual Entry</h1>
  <p><a href="/">← View public tracker</a> · <a href="/status">status</a></p>
  ${savedNotice}
  ${sections}
</body></html>`;
}

app.post('/admin/save/:slug', requireAdmin, (req, res) => {
  const { slug } = req.params;
  const contest = store.seedContests.find((c) => c.slug === slug);
  if (!contest) {
    res.status(404).send('Unknown race');
    return;
  }
  const votes = {};
  contest.candidates.forEach((cand, i) => {
    const val = req.body[`votes_${i}`];
    if (val !== undefined) votes[cand.name] = val === '' ? null : Number(val);
  });
  try {
    store.updateContest(slug, {
      precinctsReporting: req.body.precinctsReporting,
      totalPrecincts: req.body.totalPrecincts,
      votes,
    });
  } catch (err) {
    res.status(400).send(escapeHtml(err.message));
    return;
  }
  res.redirect(`/admin?saved=${encodeURIComponent(slug)}`);
});

app.get('/admin', requireAdmin, (req, res) => {
  res.send(renderAdminPage(req, req.query.saved || null));
});

app.listen(PORT, () => {
  if (!ADMIN_PASSWORD) {
    console.warn('WARNING: ADMIN_PASSWORD not set — /admin is disabled. Set it via env var to enable manual entry.');
  }
  console.log(`MN manual-entry tracker listening on :${PORT}`);
});
