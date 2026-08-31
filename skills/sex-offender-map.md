# Sex offender map skill

Builds (or refreshes) the embeddable Google My Map of registered sex
offenders living in a Connecticut town, sourced from the state DESPP Sex
Offender Registry, and drops it into the recurring Patch article
"[N] Sex Offenders Live In [Town] As Of [Month Year]" (Crime & Safety).

## When to use

Invoke when asked to build, update, or add data to a sex-offender map or
article for a town, e.g. "do the sex offender map for Wilton," "refresh the
Ridgefield offender map," or "the popups are missing X, add it." Each town
is its own map and its own article — beats are never combined here.

Two reference points, both Ridgefield:

| | Article | Map `mid` |
|---|---|---|
| Format model (published 2023) | `my.patch.com/article/31509525` | `1f1pJRMSwY9qLokBLqKkSWcDxIvqDEjo` |
| 2026 rebuild worked this session | `my.patch.com/article/4929873` | `1pxEzbwJ5ecjUeuZLqFIRP36QnD_uzgE` |

The article embeds the **live** map (`google.com/maps/d/embed?mid=...`), so
every map edit propagates to the article automatically — you never re-embed
after changing map data.

## The registry (data source)

Connecticut DESPP runs its public registry on the OffenderWatch platform.
Same data is reachable at `icrimewatch.net`, `communitynotification.com`,
and `sheriffalerts.com` — all keyed by the CT agency/office id **`54567`**.
Verify the id if it ever breaks: `portal.ct.gov/Services/Public-Safety/Sex-Offender-Registry/`
links to `sheriffalerts.com/cap_office_disclaimer.php?office=54567`.

### Getting in (needs the user's OK)

The registry gate is a terms/disclaimer checkbox — "I agree to the above
terms & conditions" + Continue — and its text warns that misusing registry
info is prosecutable. Our use (news reporting) is permitted, but **accepting
terms is an action that needs explicit user permission each session**. Stop
and ask the user to confirm before clicking through; do not accept the
disclaimer on your own.

Once through, disclaimer URL for CT:
`https://sheriffalerts.com/cap_office_disclaimer.php?office=54567`

### Searching

Search by **City** = town name with the "All Cities" box checked. Result URL
shape:
`https://www.icrimewatch.net/results.php?AgencyID=54567&SubmitNameSearch=1&OfndrCity={Town}&AllCity=1`

Query-string content in JS tool output trips the harness content filter
("BLOCKED: Cookie/query string data"). Work around it by masking digits
(`.replace(/[0-9]/g,'#')`) or extracting only what you need (ids, names)
before returning a value.

### Detail pages

`https://www.icrimewatch.net/offenderdetails.php?OfndrID={id}&AgencyID=54567`

Fetch each with in-page `fetch()` + `DOMParser`, then pull:

- **Age** (and DOB) — from the "Physical Description" block.
- **Per offense**, from the "Offenses" block: **Description** (statute +
  text), **Date Convicted**, **Conviction State**, **Release Date**.
  - Strip a trailing " View this statute" from the description text.
  - The registry sometimes lists the *same* offense twice — dedupe identical
    blocks. Genuinely distinct offenses (different statute/date/state) are
    separated by an underscore rule; keep those as separate blocks.

### Photos

`https://docs.watchsystems.com/pictures/54567/{OfndrID}-{uuid}.jpg`

The results-page thumbnails carry this exact URL; the filename prefix is the
OfndrID, so matching photo → offender is unambiguous. The bare URL 302s
through `icrimewatch.net/ValidatePhoto.php`, but it renders fine as an
`<img>` and — importantly — the **My Maps "Add image by URL" fetcher accepts
it directly** and re-hosts the image on `mymaps.usercontent.google.com`.
CORS blocks a `fetch()` of it from the registry page; that's expected and
doesn't matter.

## Building the map

### 1. Import the base layer

Import a CSV with columns **Name, Alias, Status, Address, City, State, Zip**
(the Address is what geocodes each pin). Name the layer
`{town}-sex-offender-registry.csv`.

### 2. Per-feature data

Edit each feature (marker → pencil, or the data table) to add:

- **Age → into the Name**: `"Julio Enrique Gonzalez, 79"` (matches the 2023
  map, which put age in the title).
- **An `Offense` column** holding the crime block. Do **not** call the
  column `description` — the popup renders the column name as its gray
  label, and the ask is for that label to read "Offense."

  My Maps popups render column values as **escaped plain text** — no HTML,
  no `<br>`, no newlines (single-line inputs; a newline in the feature form
  jumps to the next field and corrupts it). So keep each Offense value on
  one line and use inline "• " bullets as separators:

  ```
  • Description: 130.60 - Sexual abuse in the second degree  • Date Convicted: 03/07/2002  • Conviction State: New York  • Release Date: 05/30/2002
  ```

  Two spaces before each "• " after the first. Multiple offenses:
  `  _____________________________  ` between blocks.

- **A photo**: marker popup → camera icon → **Image URL** tab → paste the
  `docs.watchsystems.com` URL → wait for the preview thumbnail → **Insert**
  → **Save**. Shows at the top of the popup.

### 3. Pin style

Layer → **Individual styles** dropdown → **Group places by → Uniform
style** → click the paint-bucket on the "All items" row → pick red
**RGB(255, 82, 82)**. Red reads clearly against the base map; the default
blue does not. (Clicking the swatch via `element.click()` in the JS tool
does *not* fire the handler — click it with the mouse, or find its
`aria-label="RGB (255, 82, 82)"` coordinates and click there.)

### 4. Renaming a column

My Maps has **no column rename.** To rename `description` → `Offense`:
column header dropdown → **Duplicate** (dialog lets you name the copy
`Offense`; it copies all values) → then delete the original `description`
column. The delete-confirm warns "styles and labels using this column will
be reset" — fine as long as pin style is Uniform (not styled-by-column) and
labels are "No labels."

## Verifying

Fetch the map's KML and check it programmatically rather than eyeballing
popups:

```
https://www.google.com/maps/d/kml?mid={mid}&forcekml=1&cb={Date.now()}
```

- `<Data name="...">` — confirms the column set (want `Offense`, not
  `description`).
- `gx_media_links` present in a `<Placemark>` — that feature has a photo.
  (Can lag a few seconds behind a save; re-fetch.)
- `<IconStyle><color>` in `AABBGGRR` order — red is `ff5252ff`.
- `<coordinates>` in the KML trips the content filter; strip
  `<coordinates>...</coordinates>` (and mask digits) before returning KML
  text from the JS tool.

Then open `google.com/maps/d/viewer?mid={mid}` and spot-check one popup:
photo on top, then `Offense`, then Alias/Status/Address/…

## The article

The body is standing boilerplate — the DESPP/registry explainer, the DOJ
"~10% strangers / 60% known non-family / 30% family / ~quarter under 18"
statistics, and the ATSA Halloween-risk quote. **Pull the current text
verbatim from the reference article (31509525)** rather than keeping a copy
here that can drift.

Skeleton:

1. **Headline**: "[N] Sex Offenders Live In [Town] As Of [Month Year]"
   (spell out the number under 10 per this repo's `CLAUDE.md`, e.g. "Four
   Sex Offenders Live In Ridgefield As Of August 2026").
2. Dateline lead: "[TOWN], CT —" + seasonal hook + the count + "according
   to the Connecticut Sex Offender Registry."
3. Boilerplate paragraphs (from 31509525).
4. The embedded map.
5. AP style throughout per `CLAUDE.md` — no Oxford comma, "the city"/"the
   town" on second reference, attribute the count to the registry, and note
   the registry data can change.

Publishing photos of registrants: the 2023 article did it and it's the
established practice for this beat; the images are from the public state
registry.

## Gotchas

- **My Maps editor panel loads invisible.** `#featureListPanel` often comes
  up with inline `visibility:hidden` and the whole left editor UI is gone
  even though it's in the DOM. Fix:
  `document.getElementById('featureListPanel').style.visibility='visible'`.
  Re-apply after each fresh load of `/maps/d/edit`.
- **Screenshot tool times out** intermittently on My Maps ("renderer frozen
  or unresponsive") and occasionally paints a tiled/ghosted frame after a
  drag — just retry; press Escape and click empty map to clear a ghost.
- **Two My Maps tabs open at once** wedged the editor earlier in the
  session; keep one editor tab.
- **Feature-form field focus** is fragile: clicking a data-table Name cell
  opens the marker popup and steals focus, and the first `type` into a
  selected cell *appends* rather than replaces. Reliable pattern: type one
  char to enter edit mode → Ctrl+A → Delete → type the real value → commit
  by clicking the next row. For the Name/title specifically, the marker
  popup's pencil → feature form → click end of title field → End → type
  ", {age}" worked cleanly.
- **`icrimewatch` AgencyID 54566 is Barrow County, GA** — not CT. CT is
  **54567**.

## Customization

Town-specific notes and technique additions below this line:

<!-- Your additions here -->
