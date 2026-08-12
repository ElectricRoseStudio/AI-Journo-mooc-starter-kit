const { Client } = require('basic-ftp');
const { Writable } = require('stream');

const FTP_HOST = process.env.MN_FTP_HOST || 'ftp.sos.mn.gov';
const FTP_USER = process.env.MN_FTP_USER || 'media';
const FTP_PASSWORD = process.env.MN_FTP_PASSWORD || 'results';
const FTP_DIR = process.env.MN_FTP_DIR || '20260811';
const FEED_KEY = 'primary-2026';
const LABEL = '2026 Minnesota State Primary';
const SOURCE_LABEL = 'Minnesota Secretary of State (media FTP feed)';

// Each race pulls from one of the MN SOS media FTP summary files and filters
// by party (and district, for U.S. House). See ../README.md for the file
// format. Update this list if the races we're tracking change.
const RACE_CONFIG = [
  {
    slug: 'governor-republican',
    file: 'Governor.txt',
    party: 'R',
    name: 'Governor — Republican Primary',
    office: 'Governor',
    tag: 'Statewide',
  },
  {
    slug: 'us-senate-republican',
    file: 'ussenate.txt',
    party: 'R',
    name: 'U.S. Senate — Republican Primary',
    office: 'U.S. Senate',
    tag: 'Statewide',
  },
  {
    slug: 'us-senate-dfl',
    file: 'ussenate.txt',
    party: 'DFL',
    name: 'U.S. Senate — DFL Primary',
    office: 'U.S. Senate',
    tag: 'Statewide',
  },
  {
    slug: 'us-house-2-dfl',
    file: 'ushouse.txt',
    party: 'DFL',
    district: '2',
    name: 'U.S. House District 2 — DFL Primary',
    office: 'U.S. House (District 2)',
    tag: 'Congressional',
  },
];

const FILES_NEEDED = [...new Set(RACE_CONFIG.map((r) => r.file))];

const state = {
  label: LABEL,
  feedKey: FEED_KEY,
  url: `ftp://${FTP_HOST}/${FTP_DIR}/`,
  lastUpdated: null,
  fetchedAt: null,
  error: null,
  stale: false,
  staleSince: null,
  mock: false,
  contests: [],
};

// One line looks like:
// MN;;;0331;Governor & Lt Governor;;0301;John Krhin and Dennis Conn;;;R;174;4105;204;0.81;25184
// fields: state;county;-;raceCode;raceName;district;candCode;candidateName;-;-;party;precinctsReporting;totalPrecincts;votes;pct;raceTotalVotes
// Confirmed against the live 2026-08-11 feed — precinctsReporting/votes are NOT in the order the
// original doc comment assumed, and there is no boolean winner flag; the last field is the race's
// total vote count, so "winner" has to be derived (see buildContests).
function parseLine(line) {
  const f = line.split(';');
  if (f.length < 15) return null;
  return {
    raceName: f[4],
    district: f[5] || null,
    candidateName: f[7],
    party: f[10],
    precinctsReporting: Number(f[11]) || 0,
    totalPrecincts: Number(f[12]) || null,
    votes: Number(f[13]) || 0,
  };
}

function parseFile(text) {
  return text
    .split(/\r?\n/)
    .map((l) => l.trim())
    .filter(Boolean)
    .map(parseLine)
    .filter(Boolean);
}

async function downloadText(client, filename) {
  const chunks = [];
  const sink = new Writable({
    write(chunk, enc, cb) { chunks.push(chunk); cb(); },
  });
  await client.downloadTo(sink, `${FTP_DIR}/${filename}`);
  return Buffer.concat(chunks).toString('utf8');
}

function buildContests(filesText) {
  return RACE_CONFIG.map((cfg) => {
    const records = parseFile(filesText[cfg.file])
      .filter((r) => r.party === cfg.party && (!cfg.district || r.district === cfg.district));

    const totalVotes = records.reduce((sum, r) => sum + r.votes, 0);
    const maxVotes = records.reduce((max, r) => Math.max(max, r.votes), 0);

    return {
      slug: cfg.slug,
      name: cfg.name,
      office: cfg.office,
      party: cfg.party === 'DFL' ? 'DFL' : 'Republican',
      tag: cfg.tag,
      precinctsReporting: records[0] ? records[0].precinctsReporting : null,
      totalPrecincts: records[0] ? records[0].totalPrecincts : null,
      candidates: records.map((r) => ({
        name: r.candidateName,
        party: cfg.party === 'DFL' ? 'DFL' : 'Republican',
        votes: r.votes,
        pct: totalVotes > 0 ? (r.votes / totalVotes) * 100 : null,
        winner: totalVotes > 0 && r.votes === maxVotes,
      })),
    };
  });
}

async function fetchLoop() {
  const client = new Client(15_000);
  try {
    await client.access({ host: FTP_HOST, user: FTP_USER, password: FTP_PASSWORD, secure: true });

    const filesText = {};
    let newestMtime = null;
    for (const file of FILES_NEEDED) {
      filesText[file] = await downloadText(client, file);
      try {
        const info = await client.lastMod(`${FTP_DIR}/${file}`);
        if (info && (!newestMtime || info > newestMtime)) newestMtime = info;
      } catch {
        // lastMod is best-effort; not all FTP servers support MDTM reliably.
      }
    }

    state.contests = buildContests(filesText);
    state.lastUpdated = newestMtime ? newestMtime.toISOString() : new Date().toISOString();
    state.fetchedAt = new Date().toISOString();
    state.error = null;
    state.stale = false;
    state.staleSince = null;
  } catch (err) {
    state.error = err.message;
    if (!state.stale) {
      state.stale = true;
      state.staleSince = new Date().toISOString();
    }
  } finally {
    client.close();
  }
}

function start() {
  fetchLoop();
  setInterval(fetchLoop, 120_000);
}

module.exports = {
  state, start, FEED_KEY, LABEL, SOURCE_LABEL, RACE_CONFIG, parseFile, buildContests,
};
