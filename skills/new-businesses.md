# New businesses skill

Runs the CT Business Registry -> Datawrapper table -> hyperlocal article -> email
pipeline, **one article per PATCHBEAT**, for a given registration month. This is
the Claude-executed version of the workflow described for ChatGPT in
`chatgpt-business-registry-template.md`.

## When to use

Invoke when asked for a "new business roundup," "businesses registered in
[month]," or the like, e.g. "do the new business roundup for August" or
"new businesses for the Ellington-Somers beat, July 2026."

Towns, beats and editors come from **`CT_Towns.csv`** (columns
`PATCHTOWN, PATCHBEAT, EDITOR, EMAIL`). One article + one table + one headline
per **distinct PATCHBEAT**, not per town — a beat's towns are combined.

### Parsing CT_Towns.csv

- **`PATCHBEAT` is comma-delimited inside a quoted CSV field** here
  (`Windsor Locks,"Windsor Locks, East Windsor",Jay Kenney,...`) — **not**
  semicolons like the older `CT_Towns_Test*.csv` files. A standard CSV reader
  handles the quoting; then split the `PATCHBEAT` value on `,`.
- Spacing after the comma is **inconsistent** — `"Chester,Essex,Deep River"`
  (no space) vs `"Windsor Locks, East Windsor"` (space). Always `.strip()`
  every split token.
- Single-town beats have `PATCHBEAT == PATCHTOWN` (e.g. `Windsor,Windsor,...`).
- **Build beats by grouping town rows on their `PATCHBEAT` value**, not by
  guessing which towns "go together." Every town in a multi-town beat carries
  the full beat string in its own row, so grouping is exact — but the groupings
  are **not geographic intuition**. Example: `Windsor` is its own single-town
  beat; `Windsor Locks` and `East Windsor` form a separate two-town beat that
  **does not include Windsor**; `South Windsor` is a third, single-town beat. If
  a request names several adjacent towns, resolve each town's actual
  `PATCHBEAT` before deciding how many articles that is.
- The **beat string's order is the town order** for the table title, even when
  the individual town rows appear in a different order.
- Editor/EMAIL are consistent within each real multi-town beat — use any row's.
- `CT_Towns.csv` has had delimiter corruption before (git `29ccf71`). As of this
  writing it also lists the same pair as two beats in opposite orders —
  `"Woodbury,Middlebury"` (from Middlebury's row) and `"Middlebury,Woodbury"`
  (from Woodbury's row). **Dedupe beats by their town set**; if the CSV gives
  conflicting orders for one set, use alphabetical order for the title and flag
  it to the user.

## 1. Pull the data (Socrata)

Endpoint: `https://data.ct.gov/resource/n7gp-d28j.json` — fetch with `curl` /
`urllib` (not the browser; query-string data in the JS tool's output trips the
harness content filter).

```
$select = date_registration,name,billingstreet,billingcity,billingstate,naics_code
$where  = upper(billingcity) in ('TOWN1','TOWN2',...)
          AND billingstate='CT'
          AND date_registration >= 'YYYY-MM-01T00:00:00'
          AND date_registration <= 'YYYY-MM-<last day>T23:59:59'
$limit  = 5000
```

- **Always `upper(billingcity)`** — the registry stores city names in mixed and
  all caps; a plain `=` match silently drops rows (see
  `feedback_socrata_city_case.md`). The same applies when you **group the
  results by town** — normalize `billingcity` to upper before bucketing, or
  `ENFIELD` / `Enfield` / `OXFORD` / `Oxford` become separate buckets.
- `billingstate='CT'` satisfies "ensure the businesses were registered in
  Connecticut."
- One query per run covering every town in every beat is fine; group locally.

## 2. Clean the rows

- **Strip the parenthesized NAICS code**: `re.sub(r"\s*\(\d+\)\s*$", "", naics)`.
  Keep the descriptive text (including parens that are part of the category name
  itself). Result becomes the `Type` column.
- **Bare NAICS code, no label** (e.g. `naics_code == "532490"`): look up the
  official NAICS title and use it; note the substitution to the user. (Seen this
  run: Oxford's ROMEO EQUIPMENT LEASING CO LLC, `532490` -> "Other Commercial
  and Industrial Machinery and Equipment Rental and Leasing.")
- **Address == business name**: the filer put the company name in
  `billingstreet`. Drop the row — don't publish a fake address — and tell the
  user which one. (Seen this run: "Feeling Tipsy LLC," Windsor.)
- **Mojibake**: replace `�` ("�") with an apostrophe (seen: "JD�s
  AutoSpa LLC" -> "JD's AutoSpa LLC").
- **Don't dedupe** — a business can legitimately file twice with different NAICS
  codes (e.g. "B4 Property LLC," Ellington).
- **Preserve legal names verbatim**, including odd casing ("OXFORD 248 LLC,"
  "Icon enterprise Limited Liability Company," "Budget cleaning LLC").

## 3. Build the table

Columns and header renames:

| Beat type | Columns (in order) |
|---|---|
| single-town | `Business` (name), `Address` (billingstreet), `Type` (cleaned naics_code) |
| multi-town  | `Business`, `City` (billingcity), `Address`, `Type` |

**Sort rows alphabetically by `Business`** (`key=str.casefold`). Digit-first
names ("12 Riverview Road, LLC," "266 North Main LLC") sort above the letters —
that's expected.

Title:

- single-town: `"{Town} Businesses Registered in {Month Year}"`
- multi-town: town list joined by `", "` with the **last two joined by `" & "`**:
  `"A, B & C Businesses Registered in {Month Year}"` (two towns -> `"A & B ..."`).

## 4. Datawrapper (API)

Base `https://api.datawrapper.de/v3`, header `Authorization: Bearer <key>`.
**The API key is a live credential the user supplies per run — never write it
into this file or any committed script.**

1. `POST /charts` — body `{"title": "<table title>", "type": "tables"}` -> `id`.
2. `PUT /charts/{id}/data` — `Content-Type: text/csv`, raw CSV body.
3. `PATCH /charts/{id}` — merge-patch:
   ```json
   {"metadata": {
     "describe": {
       "intro": "",
       "source-name": "Connecticut Business Registry",
       "source-url": "https://data.ct.gov/Business/Connecticut-Business-Registry-Business-Master/n7gp-d28j/about_data"
     },
     "visualize": {
       "striped": true,
       "perPage": 200,
       "mobileFallback": true, "mobile-fallback": true,
       "compactMode": true, "compact-layout": true, "compactLayout": true
     }
   }}
   ```
   - `striped` and `perPage` are **confirmed** (toggled and eyeballed this run).
     Datawrapper tables **silently truncate at 20 rows** with no "show more"
     control unless `perPage` is set high enough — critical, several beats
     exceed 20.
   - The mobile-fallback / compact keys couldn't be pinned to a single exact
     name (this harness's `resize_window` doesn't change the screenshot
     viewport, so the card-layout swap can't be eyeballed). Setting every
     plausible variant is harmless — unknown keys are stored and ignored.
4. `POST /charts/{id}/publish` -> `data.publicUrl`
   (`https://datawrapper.dwcdn.net/{id}/{version}/`). **Every publish bumps the
   version** (`/1/`, `/2/`, ...); older versions stay live at their pinned URL,
   so bump the embed `src` when you republish.

### Measuring the real embed height

Charts built purely via API never get their height measured server-side —
`metadata.publish.embed-height`, `/charts/{id}/embed-codes` and `/v3/oembed`
all keep returning the `400` default. Load the published charts in an iframe
harness at `width="600"` and capture the `datawrapper-height` postMessage:

```js
// paste into any page's console (or javascript_tool), then read window.__h
const ids = ["ID1","ID2","..."];               // /{v}/ = the published version
document.body.innerHTML = ids.map(id =>
  `<iframe id="f-${id}" src="https://datawrapper.dwcdn.net/${id}/1/" width="600" height="400"></iframe>`
).join("");
window.__h = {};
addEventListener("message", e => {
  const h = e.data && e.data["datawrapper-height"]; if (!h) return;
  for (const k in h) for (const fr of document.querySelectorAll("iframe"))
    if (fr.contentWindow === e.source) window.__h[fr.id.slice(2)] = h[k];
});
// wait ~5s, then: JSON.stringify(window.__h)
```

## 5. Embed code (Patch CMS / Redactor format)

```html
<figure class="r-embed">
    <div class="embed-content">
        <iframe title="{TITLE}" aria-label="Table" id="datawrapper-chart-{ID}" src="https://datawrapper.dwcdn.net/{ID}/{VERSION}/" scrolling="no" frameborder="0" style="border: none;" width="600" height="{MEASURED_HEIGHT}" data-external="1"></iframe>
    </div>
</figure>
```

- Build the `title` attribute yourself with a single `&amp;` for any `&` —
  Datawrapper's own generated embed code double-encodes it (`&amp;amp;`).
- If starting from an embed copied out of Patch, drop the
  `<span class="redactor-selection-marker">` — editor cruft, not part of the embed.

## 6. The article

One per beat, 2-3 paragraphs, table as the centerpiece (embed follows the prose).

- **Audience knows the town is in Connecticut — never put the state next to a
  town name.** No `TOWN, CT —` dateline. ("Connecticut Business Registry" as the
  dataset's name is fine.)
- Para 1 — lead: total count + month; for multi-town, the per-town split and the
  dominant NAICS categories.
- Para 2 — 2-4 named businesses spanning different categories, **including the
  one spotlighted in the headline**; plus the LLC ratio. Count LLCs with
  `re.search(r"\bL\.?L\.?C\.?\b|Limited Liability Compan|Limited Liability Co\b", name, re.I)`;
  call out PLLCs and corporations (e.g. "Inc.," a booster club) as the exceptions.
- Para 3 — caveat: a registry filing doesn't mean the business has opened (can
  come well before or after); "the full list ... is below."
- AP style per this repo's `CLAUDE.md`: no Oxford comma; spell out numbers under
  10 (and any sentence-initial number); no editorializing; avoid the AI-ism list.

## 7. Headline + meta

- **Headline**: <=109 characters; **capitalize the first letter of every word**
  (including "In," "Among," "The" — stricter than AP headline case); spotlight
  one business with an unusual category. Spell out numbers under 10 (Four, Five,
  Eight), numerals for 10+, even in the headline.
  - single-town: `"Horse Farm Among 33 New Businesses Registered In Enfield In August"`
  - multi-town: `"... In A, B & C In August"`
- **Meta description**: **strictly under 136 characters.**

Validate both lengths programmatically before sending.

## 8. Email (SendGrid)

Group beats by editor EMAIL; send **each editor one email** covering their
beats, each beat's article under an `<h2>`, each beat's standalone article HTML
attached individually (same shape as the priciest-houses send).

- Credentials: `source ~/.config/newtown-mail.env` for `SMTP_HOST/PORT/USER/PASS/FROM`
  (SendGrid SMTP). Send with `smtplib` + STARTTLS.
- Each section: headline, meta description, article body, the rendered
  `<figure>` embed, and the embed code in a `<pre>` block for copy-paste.
- **Verify delivery via the SendGrid Activity API, not local logs**
  (`project_sendgrid_activity_verification.md`):
  `GET https://api.sendgrid.com/v3/messages?limit=5&query=to_email%3D%22<addr>%22`
  with `Authorization: Bearer $SMTP_PASS`. Expect ~15-60s indexing lag; look for
  `"status": "delivered"`.
- Emailing on the user's behalf normally needs confirmation, but a run of this
  skill that explicitly says "email the articles" is the go-ahead. Note: the
  auto-mode permission classifier may still block the `smtplib` send on the
  first try — if so, tell the user and let them re-approve / add a Bash rule.

## Customization

Add month- or beat-specific notes and technique additions below this line:

<!-- Your additions here -->

### August 2026 run (test file `CT_Towns-Test-02.csv`, not `CT_Towns.csv`)

Six beats, one email to rich.kirby@patch.com (all editors in that test file were
Rich Kirby). 138 registrations after dropping one bad-address row. Published
Datawrapper charts (version /2/ carries the alpha sort + source annotation):

| Beat | id | rows | height@600 |
|---|---|---|---|
| Enfield | `DzUrl` | 33 | 1500 |
| Ellington & Somers | `lQGNj` | 20 | 1117 |
| Oxford | `MePzF` | 18 | 999 |
| Windsor, Windsor Locks & East Windsor | `6Y0KU` | 44 | 2510 |
| Suffield | `0PjRl` | 10 | 511 |
| Granby & East Granby | `QU0oA` | 13 | 841 |

Prior months' roundups live in the same Datawrapper account
(`GET /v3/charts?limit=50&orderBy=createdAt` — the July 2026 batch is there);
check for an existing chart before creating a duplicate when re-running a month.
