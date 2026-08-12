# Minnesota 2026 Primary Tracker — Manual Entry (standby)

Same four races, same look, same embed/status/API routes as `../mn-primary-2026/`, but instead
of polling a feed, a password-protected `/admin` page lets Patch staff type in vote counts by
hand. Kept on standby for the likely case that AP Elections API access or an official MN SecState
feed doesn't come through in time.

- Governor — Republican Primary
- U.S. Senate — Republican Primary
- U.S. Senate — DFL Primary
- U.S. House District 2 — DFL Primary

## How it works

- `data/mn-races-seed.json` holds the fixed candidate roster per race (same data as the
  automated tracker) — not editable from `/admin`.
- `/admin` has one form per race: precincts reporting / total precincts, and a vote count input
  per candidate. Submitting a race's form recomputes percentages and writes to
  `data/mn-manual-results.json` on disk immediately.
- The public `/`, `/status`, `/mn/:feedKey/race/:slug`, and `/api/mn/:feedKey/*` routes read from
  that same file, merged with the roster — identical shape to the automated tracker's routes, so
  existing embed codes/integrations work the same way if you ever swap which service is live.

## Auth

`/admin` requires HTTP Basic Auth. Set at deploy time:

```bash
gcloud run deploy mn-primary-2026-manual --image ... \
  --set-env-vars ADMIN_USER=patch,ADMIN_PASSWORD='choose-a-real-password'
```

If `ADMIN_PASSWORD` isn't set, `/admin` responds 503 rather than allowing anonymous edits — the
service intentionally refuses to boot an open admin panel. This is a lightweight shared-password
gate for a small internal team on election night, not hardened multi-user auth — don't expose the
password outside the team, and rotate/retire it after the election.

## Important limitation: local-disk persistence

Results are written to a JSON file on the container's local disk, not to a database or bucket.
That means:

- **Fine for a single Cloud Run instance.** Deploy with `--max-instances 1` so every request —
  public reads and admin writes — hits the same instance and sees the same data.
- **Data is lost on redeploy or cold restart.** Cloud Run wipes local disk when an instance is
  replaced. If you need to survive that (e.g. a code change mid-election night), back up
  `data/mn-manual-results.json` periodically (`gcloud run services proxy` + curl `/debug`, or add
  a scheduled export) and consider upgrading to a Cloud Storage-backed store before election night
  if durability matters more than the extra setup.

This tradeoff was chosen for simplicity, matching how the other single-instance Cloud Run trackers
in this project are already deployed. If you want the more robust GCS-backed version instead, ask
and it can be built out.

## Local dev

```bash
cd mn-primary-2026-manual
npm install
ADMIN_PASSWORD=test123 node server.js
# visit http://localhost:8080  (public)
# visit http://localhost:8080/admin  (user: patch, password: test123)
```

## Deploy

```bash
docker build -t us-west1-docker.pkg.dev/patch-elections-renee-2026/cloud-run-source-deploy/mn-primary-2026-manual:latest .
docker push us-west1-docker.pkg.dev/patch-elections-renee-2026/cloud-run-source-deploy/mn-primary-2026-manual:latest
gcloud run deploy mn-primary-2026-manual \
  --image us-west1-docker.pkg.dev/patch-elections-renee-2026/cloud-run-source-deploy/mn-primary-2026-manual:latest \
  --region us-west1 --platform managed --allow-unauthenticated --port 8080 \
  --max-instances 1 \
  --set-env-vars ADMIN_USER=patch,ADMIN_PASSWORD='choose-a-real-password'
```

## Routes

- `/` — main tracker page (race cards)
- `/admin` — password-protected manual entry form
- `/status` — race list, embed URLs, last-entry time
- `/debug` — raw in-memory state as JSON
- `/embeds.html` — copyable iframe embed codes per race
- `/api/mn/primary-2026/index` — normalized contest list (no candidate detail)
- `/api/mn/primary-2026/raw` — full state including candidates/votes
- `/mn/primary-2026/race/:slug` — single embeddable race results panel
