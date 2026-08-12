const fs = require('fs');
const path = require('path');

const SEED_PATH = path.join(__dirname, '..', 'data', 'mn-races-seed.json');
const RESULTS_PATH = process.env.MANUAL_RESULTS_PATH
  || path.join(__dirname, '..', 'data', 'mn-manual-results.json');

function loadSeed() {
  return JSON.parse(fs.readFileSync(SEED_PATH, 'utf8')).contests;
}

function emptyContestResult() {
  return { precinctsReporting: null, totalPrecincts: null, updatedAt: null, votes: {} };
}

function loadResults(seedContests) {
  let parsed;
  try {
    parsed = JSON.parse(fs.readFileSync(RESULTS_PATH, 'utf8'));
  } catch (err) {
    parsed = { contests: {} };
  }
  if (!parsed.contests) parsed.contests = {};
  for (const c of seedContests) {
    if (!parsed.contests[c.slug]) parsed.contests[c.slug] = emptyContestResult();
  }
  return parsed;
}

function saveResults(results) {
  fs.mkdirSync(path.dirname(RESULTS_PATH), { recursive: true });
  fs.writeFileSync(RESULTS_PATH, JSON.stringify(results, null, 2));
}

const seedContests = loadSeed();
let resultsData = loadResults(seedContests);

function getMergedContests() {
  return seedContests.map((c) => {
    const r = resultsData.contests[c.slug] || emptyContestResult();
    const votesMap = r.votes || {};
    const totalVotes = Object.values(votesMap).reduce((sum, v) => sum + (Number(v) || 0), 0);
    return {
      slug: c.slug,
      name: c.name,
      office: c.office,
      party: c.party,
      tag: c.tag,
      precinctsReporting: r.precinctsReporting ?? null,
      totalPrecincts: r.totalPrecincts ?? null,
      updatedAt: r.updatedAt ?? null,
      candidates: c.candidates.map((cand) => {
        const raw = votesMap[cand.name];
        const votes = raw === undefined || raw === null || raw === '' ? null : Number(raw);
        return {
          name: cand.name,
          party: cand.party,
          votes,
          pct: votes !== null && totalVotes > 0 ? (votes / totalVotes) * 100 : null,
        };
      }),
    };
  });
}

function getContest(slug) {
  return getMergedContests().find((c) => c.slug === slug) || null;
}

function updateContest(slug, { precinctsReporting, totalPrecincts, votes }) {
  if (!resultsData.contests[slug]) throw new Error(`Unknown race: ${slug}`);
  const toNumOrNull = (v) => (v === '' || v === null || v === undefined ? null : Number(v));
  resultsData.contests[slug] = {
    precinctsReporting: toNumOrNull(precinctsReporting),
    totalPrecincts: toNumOrNull(totalPrecincts),
    updatedAt: new Date().toISOString(),
    votes,
  };
  saveResults(resultsData);
}

function lastUpdated() {
  const times = Object.values(resultsData.contests).map((c) => c.updatedAt).filter(Boolean);
  return times.length ? times.sort().slice(-1)[0] : null;
}

function anyDataEntered() {
  return Object.values(resultsData.contests).some((c) => Object.keys(c.votes || {}).length > 0);
}

module.exports = {
  seedContests, getMergedContests, getContest, updateContest, lastUpdated, anyDataEntered,
};
