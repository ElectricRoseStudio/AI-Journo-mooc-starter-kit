# Minnesota 2026 Primary Tracker

Tracks four August 11, 2026 Minnesota primary races, live:

- Governor — Republican Primary
- U.S. Senate — Republican Primary
- U.S. Senate — DFL Primary
- U.S. House District 2 — DFL Primary

Built to match the look and architecture of the Maryland and Virginia trackers described in
`../election-scraper-deployment-guide.md`.

## Data source: MN SOS media FTP feed

`electionresults.sos.mn.gov` (the public results website) sits behind Radware bot detection —
scripted HTTP requests get redirected to a JS challenge, so it can't be polled directly. Instead,
this tracker uses the Secretary of State's **media FTP site**, which MN OSS provides specifically
for automated/press access and isn't behind the web bot-wall:

```
Host:     ftp.sos.mn.gov
Protocol: FTPS (explicit encryption / AUTH TLS — not SFTP)
User:     media
Password: results
Dir:      20260811   (the election-date subdirectory; changes every election)
```

These are the standard credentials MN SOS distributes to any member of media — not secret to
Patch specifically, but still kept as overridable env vars (`MN_FTP_HOST`, `MN_FTP_USER`,
`MN_FTP_PASSWORD`, `MN_FTP_DIR`) rather than hardcoded, in case they're rotated or the directory
naming changes for a future election.

Files on the FTP site are updated approximately every 10 minutes by MN SOS; this service polls
every 2 minutes. `lib/results.js` downloads three summary files per cycle:

- `Governor.txt` → governor-republican (filtered to party `R`)
- `ussenate.txt` → us-senate-republican (`R`) and us-senate-dfl (`DFL`)
- `ushouse.txt` → us-house-2-dfl (`DFL`, district `2`)

### File format

Each line is semicolon-delimited:

```
MN;<county>;;<raceCode>;<raceName>;<district>;<candCode>;<candidateName>;;;<party>;<precinctsReporting>;<totalPrecincts>;<votes>;<pct>;<raceTotalVotes>
```

Example: `MN;;;0331;Governor & Lt Governor;;0301;John Krhin and Dennis Conn;;;R;174;4105;204;0.81;25184`

Confirmed against the live 2026-08-11 feed. Note this differs from an earlier assumed layout:
`votes` and `precinctsReporting` are in the opposite order you'd guess from field names alone, and
the last field is the race's total vote count, not a boolean winner flag — there is no winner flag
in this feed, so `buildContests()` derives "winner" as whichever candidate currently has the most
votes.

Candidate rosters are **not** stored separately — they're derived fresh from these files on every
poll, so there's no seed data that can drift out of sync with the ballot. If MN SOS changes this
file layout, update `parseLine()` in `lib/results.js` to match.

Lines use CRLF (`\r\n`) endings; `parseFile()` handles that.

Note: MN SOS asks that public links to the ENR *website* not be shared before 2 p.m. on election
day (it may hold test data until then) — this doesn't apply to the FTP feed itself, which we're
polling for real starting when summary files populate at 8 p.m. election night.

## Local dev

```bash
cd mn-primary-2026
npm install
node server.js
# visit http://localhost:8080
```

## Deploy

```bash
docker build -t us-west1-docker.pkg.dev/patch-elections-renee-2026/cloud-run-source-deploy/mn-primary-2026:latest .
docker push us-west1-docker.pkg.dev/patch-elections-renee-2026/cloud-run-source-deploy/mn-primary-2026:latest
gcloud run deploy mn-primary-2026 --image us-west1-docker.pkg.dev/patch-elections-renee-2026/cloud-run-source-deploy/mn-primary-2026:latest --region us-west1 --platform managed --allow-unauthenticated --port 8080
```

Override FTP settings at deploy time if needed, e.g. for a future election's directory:

```bash
gcloud run deploy mn-primary-2026 --image ... --set-env-vars MN_FTP_DIR=20261103
```

## Routes

- `/` — main tracker page (race cards)
- `/status` — scraper status, embed URLs, last fetch/error state
- `/debug` — raw in-memory state as JSON
- `/embeds.html` — copyable iframe embed codes per race
- `/api/mn/primary-2026/index` — normalized contest list (no candidate detail)
- `/api/mn/primary-2026/raw` — full state including candidates/votes
- `/mn/primary-2026/race/:slug` — single embeddable race results panel
