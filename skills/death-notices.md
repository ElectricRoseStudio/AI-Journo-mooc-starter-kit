# Death notices skill

Retrieves recent obituaries from local funeral home listing pages and converts them into short, publishable Death Notices.

## When to use

Invoke this skill when asked to pull obituaries/death notices for a town, e.g. "give me death notices for Waterford."

## Process

1. Look up the requested town in `FuneralHomes.csv` to get its funeral home(s) and obituary listing URL(s). A town may have no funeral home of its own and rely on homes in neighboring towns — the `Notes` column flags this with "No Funeral Home in Town."
2. Fetch each listing URL. Some funeral home sites block automated fetches (403, usually Cloudflare) or render listings via JS/carousel with no server-side content — note these as unreachable rather than guessing at their contents. Dignity Memorial properties (`dignitymemorial.com/obituaries?locationcode=...`) are Cloudflare-protected against direct curl/raw HTTP requests but are reachable via WebFetch, which returns real listing data — try WebFetch before writing a Dignity Memorial URL off as unreachable.
3. From the reachable listings, keep only decedents whose residence matches the requested town (funeral homes often serve multiple towns).
4. Drop any entry missing a usable age or date rather than guessing — note it as skipped for incomplete data.
5. Sort by date of death, most recent first, and take the requested count (default 4-6).
6. Convert each into a Death Notice using the format below, in AP style per this repo's `CLAUDE.md` (no Oxford comma, numerals for ages, spell out other numbers under 10).

## Death Notice format

```
[Full Name], [age], died [Month Day]. [One factual sentence: occupation/service/defining detail]. [Second factual sentence: occupation/service/defining detail] [Third factual sentence: occupation/service/defining detail][Fourth factual sentence: survived by / key family, if available]. Source: [URL link to obituary]
```

- Keep each notice to 3-4 sentences — this is a notice, not a full obituary.
- Do not editorialize or add sentiment not present in the source ("beloved," "cherished") unless quoting the source directly.


## Customization

Add town-specific or style additions below this line:

<!-- Your additions here -->

### Cody-White Funeral Home (Milford, CT) — serves Orange, Milford, West Haven

`https://www.codywhitefuneralservice.com/obituaries` renders via a Vue SPA (Carriage
Services' "TributeCenterOnline" platform) with no server-side content, so a plain
fetch/WebFetch of that URL returns only the page shell — do not mark it unreachable,
query the JSON API behind it instead:

```
GET https://api.secure.tributecenteronline.com/ClientApi/obituaries/GetObituariesExtended
    ?pageNumber=1&pageSize=100&searchTerm=&sortingColumn=3&servingLocationId=0
Header: DomainId: 5005a7e5-15a7-40e7-a7fb-addef6fad565
```

Returns JSON with `FullName`, `DeathDate`, `BirthDate`, `Id`, and a full HTML
`Description` field (the obituary body). `PlaceOfResidence` is consistently null —
determine the decedent's town from the opening line of `Description` instead
(pattern: "`Name, age, of TOWN,`"). Watch for false positives where "Orange" (or
another town name) appears elsewhere in the text as a birthplace, a facility name
(e.g. "Maplewood at Orange"), or a past-residence mention rather than the stated
current residence — check the opening sentence, not just a keyword match.

### Lester Gee Funeral Home (New London, CT) — serves Waterford

`https://www.lestergeefh.com/obituary-listing` is on the same FrontRunner
platform as Adzima (same `runtime/311039` backend), so the same
`get-records-additional.php` API pattern applies:

```
POST https://obituaries.frontrunnerpro.com/runtime/311039/ims/WF2/public/get-records-additional.php
Body (form-encoded): pageNum=1&rpp=20&type=all&guid=380022:MainSite&wholeSite=true
```

(`guid` is base64-decoded from `window.Parameters.ExternalUid` in the page
HTML — fetch with curl, not WebFetch, to see it — decodes to `380022:MainSite`.)
Verified 2026-08-17: the API responds correctly (`{"success":true,...}`) but
`data` came back empty for `type=current` and `type=all`, and with/without
`getServiceType`/`template` params. Site's own widget config
(`data-widget-config` on the page, also base64) confirms these are the right
defaults, so this reads as genuinely no obituaries posted, not a broken query
— but re-check the `data` array on future runs before assuming that.

### Byles-MacDougall, Impellitteri-Malia, Thomas L. Neilan & Sons (serve Waterford)

All three (`byles.com`, `impellitterimaliafh.com`, `neilanfuneralhome.com`)
are Cloudflare-protected — confirmed via response headers (`server: cloudflare`,
`__cf_bm` cookie) on a 403 from both WebFetch and curl with a browser user
agent. No JS-API workaround found yet (unlike the FrontRunner/TributeCenter
sites above). Note as unreachable rather than retrying with different headers.

### Hull Funeral Service / Colonial Funeral Home (New Milford, CT) — serves New Milford

`https://www.hullfuneralservice.com/listings` is also Cloudflare-protected —
same signature (`server: cloudflare`, `__cf_bm` cookie, 403 on both WebFetch
and curl with a browser user agent), confirmed 2026-08-17. Note as unreachable;
use Lillis Funeral Home (`lillisfuneralhome.wordpress.com/obituaries/`, a
plain WordPress page, fetches fine) as the other New Milford source.

### Fulton-Theroux Funeral Service (Old Lyme, CT, Dignity Memorial) — serves Lyme, Old Lyme

`https://www.dignitymemorial.com/obituaries?locationcode=2694` is Cloudflare-protected
against direct HTTP — curl (even with a browser user agent) gets a 403 with
`server: cloudflare` and a `__cf_bm` cookie. WebFetch, however, gets through and
returns a real, complete listing (confirmed 2026-08-17) — use WebFetch first for
any Dignity Memorial URL rather than assuming it's unreachable; only fall back to
"unreachable" if WebFetch itself comes back empty or blocked.

Individual obituary pages load fine via WebFetch too. Watch for the same
past-residence false positive noted for Cody-White/Orange: the listing page's
per-entry "Residence" field can reflect the requested town's funeral-home service
area rather than current residence — e.g. a listing on the Old Lyme page had
"Residence: Bozrah, CT (formerly Old Lyme, CT)" in the obituary body. Check the
individual obituary's stated current residence, not just the listing-page filter,
before including an entry.

Individual obituary page (for the Source link) follows this pattern:
`https://www.codywhitefuneralservice.com/obituaries/{First}-{Middle}-{Last}?obId={Id}`
(periods stripped from middle initials, spaces in surnames become hyphens). Verify
with a HEAD/GET before using — construct from the `FirstName`/`MiddleName`/`LastName`
fields in the API response, not from `FullName`.

The `DomainId` is specific to Cody-White; if the same platform shows up for another
funeral home (same `tributecenteronline.com`/`site-builder` JS bundle structure),
find its `window.API.domainId` by fetching the home page HTML directly with curl
(not WebFetch) and grepping for `window.API.domainId`.

### Adzima Funeral Home (Derby, CT) — serves Oxford

`https://www.adzimafh.com/obituary-listing` is another JS/AJAX-rendered listing
(FrontRunner Professional platform) — plain fetch returns only unpopulated
`{name}`/`{date}` template markup, not actual records. The live API is:

```
POST https://obituaries.frontrunnerpro.com/runtime/311039/ims/WF2/public/get-records-additional.php
Body (form-encoded): pageNum=1&rpp=20&type=current&guid=356489:11503&wholeSite=true
```

(`guid` is the site's `ExternalUid`, base64-decoded from the page HTML —
`window.Parameters.ExternalUid`.) This returns well-formed JSON
(`{"success":true,"data":[...],"maxPages":N}`), confirming the API path is
correct, but as of 2026-08-15 it came back with an empty `data` array for both
`type=current` and `type=all` — either the funeral home genuinely has no
obituaries posted right now, or the request is missing a required param
(`template`/`getServiceType`) that's set dynamically client-side and wasn't
findable via static grep of the page HTML. Re-check the `data` array before
trusting an empty result as "no obituaries" — don't assume the API is broken
just because one query came back empty.
