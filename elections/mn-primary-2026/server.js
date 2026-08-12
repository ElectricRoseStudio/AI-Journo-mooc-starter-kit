const express = require('express');
const path = require('path');
const results = require('./lib/results');

const app = express();
app.set('trust proxy', true);
const PORT = process.env.PORT || 8080;

app.use(express.static(path.join(__dirname, 'public')));

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

function contestIndexPayload() {
  return {
    label: results.state.label,
    url: results.state.url,
    lastUpdated: results.state.lastUpdated,
    fetchedAt: results.state.fetchedAt,
    error: results.state.error,
    stale: results.state.stale,
    staleSince: results.state.staleSince,
    mock: results.state.mock,
    contests: results.state.contests.map((c) => ({
      slug: c.slug,
      name: c.name,
      office: c.office,
      party: c.party,
      tag: c.tag,
      precinctsReporting: c.precinctsReporting,
      totalPrecincts: c.totalPrecincts,
    })),
  };
}

app.get('/api/mn/:feedKey/index', (req, res) => {
  res.json(contestIndexPayload());
});

app.get('/api/mn/:feedKey/raw', (req, res) => {
  res.json(results.state);
});

app.get('/mn/:feedKey/race/:slug', (req, res) => {
  const contest = results.state.contests.find((c) => c.slug === req.params.slug);
  const fetchedLabel = results.state.fetchedAt
    ? new Date(results.state.fetchedAt).toLocaleString('en-US', { timeZone: 'America/Chicago' }) + ' CT'
    : 'pending';

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
    : 'Precinct reporting: pending live feed';

  const staleBanner = results.state.stale
    ? `<div class="stale-banner">⚠ Stale data — live feed down, retrying since ${escapeHtml(results.state.staleSince || '')}</div>`
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
  <div class="sublabel">${results.SOURCE_LABEL} · Fetched: ${escapeHtml(fetchedLabel)}</div>
</div>
${staleBanner}
<table>
  <thead><tr><th>Candidate</th><th>Party</th><th>Votes</th><th>%</th></tr></thead>
  <tbody>${rows}</tbody>
</table>
<div class="table-foot">
  <div class="precinct-line">${escapeHtml(precinctLine)}</div>
  <div>Source: ${results.SOURCE_LABEL}</div>
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

app.get('/status', (req, res) => {
  const s = results.state;
  const rows = s.contests.map((c) => `<tr><td>${escapeHtml(c.name)}</td>
    <td><code>${req.protocol}://${req.get('host')}/mn/${results.FEED_KEY}/race/${c.slug}</code></td>
    <td><a href="/mn/${results.FEED_KEY}/race/${c.slug}" target="_blank">preview ↗</a></td></tr>`).join('');

  res.send(`<!DOCTYPE html><html><head><meta charset="UTF-8">
<title>MN Election Scraper — Status</title>
<style>
  body{font-family:Helvetica,Arial,sans-serif;max-width:960px;margin:40px auto;color:#1a1a1a;padding:0 20px;}
  table{width:100%;border-collapse:collapse;margin-top:10px;}
  td,th{padding:8px 10px;border-bottom:1px solid #ddd;font-size:13px;text-align:left;}
  code{background:#f4f1ea;padding:2px 6px;border-radius:3px;font-size:11.5px;}
  pre{background:#f4f1ea;padding:16px;border-radius:4px;font-size:12px;overflow:auto;white-space:pre-wrap;}
  .mock{background:#fff8e1;border:1px solid #f0c040;color:#7a5f00;padding:10px 14px;border-radius:4px;font-size:13px;}
</style>
</head><body>
  <h1>Minnesota 2026 Primary — Scraper Status</h1>
  <p>Polling <code>${escapeHtml(s.url)}</code> every 2 minutes.${s.stale ? ` <span style="color:#b8860b;">⚠ Stale — last successful fetch failed, retrying.</span>` : ''}</p>
  <p><a href="/debug">/debug</a> · <a href="/api/mn/${results.FEED_KEY}/raw">/api/mn/${results.FEED_KEY}/raw</a> · <a href="/">index</a></p>
  <h2>${escapeHtml(s.label)}</h2>
  <p>${s.contests.length} contests loaded · Last updated: ${escapeHtml(s.lastUpdated || 'unknown')} · Fetched: ${escapeHtml(s.fetchedAt || 'pending')}${s.error ? ` · <span style="color:#b3261e">Error: ${escapeHtml(s.error)}</span>` : ''}</p>
  <table><thead><tr><th>Race</th><th>Embed URL</th><th></th></tr></thead><tbody>${rows}</tbody></table>
  <h2>Embed example</h2>
  <pre>&lt;iframe src="${req.protocol}://${req.get('host')}/mn/${results.FEED_KEY}/race/SLUG"
  width="600" height="600" frameborder="0" scrolling="auto" style="border:none;"&gt;
&lt;/iframe&gt;</pre>
</body></html>`);
});

app.get('/debug', (req, res) => {
  res.type('application/json').send(JSON.stringify(results.state, null, 2));
});

app.get('/embeds.html', (req, res) => {
  const rows = results.state.contests.map((c) => {
    const src = `${req.protocol}://${req.get('host')}/mn/${results.FEED_KEY}/race/${c.slug}`;
    return `<div style="margin-bottom:28px;">
      <h3 style="font-family:Helvetica,Arial,sans-serif;">${escapeHtml(c.name)}</h3>
      <pre style="background:#f4f1ea;padding:14px;border-radius:4px;font-size:12px;overflow:auto;">&lt;iframe src="${escapeHtml(src)}"
  width="600" height="600" frameborder="0" scrolling="auto" style="border:none;"&gt;
&lt;/iframe&gt;</pre>
    </div>`;
  }).join('');
  res.send(`<!DOCTYPE html><html><head><meta charset="UTF-8"><title>Embed codes — MN 2026 Primary</title></head>
  <body style="max-width:800px;margin:40px auto;font-family:Helvetica,Arial,sans-serif;color:#1a1a1a;padding:0 20px;">
  <h1>Embed codes</h1>
  <p><a href="/">← Back to tracker</a></p>
  ${rows}
  </body></html>`);
});

app.listen(PORT, () => {
  results.start();
  console.log(`MN election tracker listening on :${PORT}`);
});
