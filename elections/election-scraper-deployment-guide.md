# Election Results Scraper — Deployment Guide
*Patch.com | Project: patch-elections-renee-2026*

---

## Overview

This guide documents how to build and deploy live election results embed systems to Google Cloud Run. We've built scrapers for Maryland, Virginia (Dem + Rep primaries), and are expanding to new states.

**Google Cloud Project:** `patch-elections-renee-2026`
**Deploy Region:** `us-west1`
**Account:** `Renee.Schiavone@patch.com`

---

## Live Services

| Service | URL | Feed |
|---|---|---|
| Maryland | https://md-election-results-47311580071.us-west1.run.app | MD SBE scraper |
| VA Dem Primary | https://va-dem-primary-2026-47311580071.us-west1.run.app | VA ENR JSON |
| VA Rep Primary | https://va-rep-primary-2026-47311580071.us-west1.run.app | VA ENR JSON |

---

## Architecture

Two approaches depending on the state:

### Approach 1: JSON Feed (Virginia-style)
Use when the state publishes a machine-readable JSON feed.
- Poll the feed every 60 seconds
- Parse contests and candidates from the JSON
- Serve embeddable race pages at `/va/:feedKey/race/:slug`

### Approach 2: HTML Scraper (Maryland-style)
Use when no JSON feed exists — scrape the official results HTML page.
- Use Cheerio to parse tables
- Re-host results as embeddable pages
- Scrape every 2 minutes

---

## Finding the JSON Feed

1. Go to the state's official results page
2. Press **F12** → **Network tab** → **Fetch/XHR filter**
3. Refresh the page
4. Look for a large request (40KB+) — that's the results data
5. Click it → **Headers** → copy the **Request URL**

**Virginia's feed URLs:**
- Statewide Dem: `https://enr.elections.virginia.gov/cdn/results/virginia/export-2026-August-Democratic-Primary.json`
- Statewide Rep: `https://enr.elections.virginia.gov/cdn/results/virginia/export-2026-August-Republican-Primary.json`
- County-level: `https://enr.elections.virginia.gov/results/public/api/elections/[county-name]/2026-August-Democratic-Primary/data`

**Virginia JSON Schema:**
```json
{
  "electionDate": "2026-08-04",
  "electionName": "2026 August Democratic Primary",
  "createdAt": "2026-08-04T23:42:51Z",
  "results": {
    "ballotItems": [
      {
        "name": "Member, United States Senate",
        "precinctsParticipating": null,
        "precinctsReporting": null,
        "ballotOptions": [
          {
            "name": "Candidate Name",
            "voteCount": 12345,
            "politicalParty": "Democratic"
          }
        ]
      }
    ]
  }
}
```

---

## Deploying a New State (JSON Feed)

### Step 1: Copy the Virginia template
```bash
cp -r ~/va-dem-primary-2026 ~/[state]-[party]-primary-2026
cd ~/[state]-[party]-primary-2026
```

### Step 2: Update the feed URL and labels
```bash
sed -i 's|[OLD_FEED_URL]|[NEW_FEED_URL]|g' server.js
sed -i 's|2026 Virginia Democratic Primary|2026 [State] [Party] Primary|g' server.js
sed -i 's|2026 Virginia Democratic Primary|2026 [State] [Party] Primary|g' public/index.html
sed -i 's|dem-primary|[new-key]|g' server.js
sed -i 's|dem-primary|[new-key]|g' public/index.html
sed -i 's|Virginia Department of Elections|[State] Department of Elections|g' server.js
```

### Step 3: Check the JSON schema
After first deploy, visit `/api/va/[feedKey]/raw` and check field names. If contests aren't parsing, update the parser in `parseFeedJSON()` to match the actual field names.

**Virginia field names:**
- Contests array: `data.results.ballotItems`
- Contest name: `item.name`
- Precincts total: `item.precinctsParticipating`
- Precincts reporting: `item.precinctsReporting`
- Candidates array: `item.ballotOptions`
- Candidate name: `c.name`
- Candidate votes: `c.voteCount`
- Candidate party: `c.politicalParty`

### Step 4: Build and deploy via Cloud Shell
**Always deploy from Google Cloud Shell** (console.cloud.google.com → `>_` icon), NOT from local PowerShell, to avoid permission issues.

```bash
docker build -t us-west1-docker.pkg.dev/patch-elections-renee-2026/cloud-run-source-deploy/[service-name]:latest .
docker push us-west1-docker.pkg.dev/patch-elections-renee-2026/cloud-run-source-deploy/[service-name]:latest
gcloud run deploy [service-name] --image us-west1-docker.pkg.dev/patch-elections-renee-2026/cloud-run-source-deploy/[service-name]:latest --region us-west1 --platform managed --allow-unauthenticated --port 8080
```

### Step 5: Verify
- `/status` — shows all races and embed URLs
- `/api/va/[feedKey]/raw` — shows raw JSON from the feed
- `/debug` — shows parse results

---

## Adding a County Feed to an Existing Server

To add a county-level feed alongside a statewide one, patch `VA_FEEDS` in server.js:

```javascript
// In server.js, find VA_FEEDS array and add:
{
  key: 'arlington-county',
  label: '2026 Arlington County Democratic Primary',
  url: 'https://enr.elections.virginia.gov/results/public/api/elections/arlington-county/2026-August-Democratic-Primary/data',
},
```

Then rebuild and redeploy.

---

## Updating an Existing Deploy

Any code change requires rebuild + redeploy:
```bash
cd ~/[service-folder]
# make changes to server.js
docker build -t us-west1-docker.pkg.dev/patch-elections-renee-2026/cloud-run-source-deploy/[service-name]:latest .
docker push us-west1-docker.pkg.dev/patch-elections-renee-2026/cloud-run-source-deploy/[service-name]:latest
gcloud run deploy [service-name] --image us-west1-docker.pkg.dev/patch-elections-renee-2026/cloud-run-source-deploy/[service-name]:latest --region us-west1 --platform managed --allow-unauthenticated --port 8080
```

---

## Embed Code Format

```html
<iframe src="https://[service-url]/va/[feedKey]/race/[slug]"
  width="600" height="600" frameborder="0" scrolling="auto" style="border:none;">
</iframe>
```

Get all embed codes from the `/status` page of each service.

---

## Troubleshooting

### Container failed to start
Usually a syntax error in server.js. Check with:
```bash
node -e "require('./server.js')" 2>&1 | head -20
```

### Storage permission error on deploy
Run this once to fix bucket-level permissions:
```bash
gcloud storage buckets add-iam-policy-binding gs://run-sources-patch-elections-renee-2026-us-west1 \
  --member="serviceAccount:47311580071-compute@developer.gserviceaccount.com" \
  --role="roles/storage.objectAdmin"
```

### Feed returning 0 contests
Check `/api/va/[feedKey]/raw` and compare field names to parser. Update `parseFeedJSON()` to match actual schema.

### Timestamps showing wrong time
Virginia sends `electionDate` as date-only (no time). Use `createdAt` instead for the last updated timestamp. All times should be converted to EST:
```javascript
new Date(timestamp).toLocaleString("en-US", { timeZone: "America/New_York" }) + " EST"
```

### catroot2 error on Windows PowerShell
Run PowerShell as Administrator, or better — use Google Cloud Shell instead.

---

## Key Contacts

- **Renee Schiavone** — Renee.Schiavone@patch.com (Cloud Run Admin/Editor)
- **Jason Antheunis** — jason.antheunis@patch.com (Project Owner — needed for project-level IAM changes)
