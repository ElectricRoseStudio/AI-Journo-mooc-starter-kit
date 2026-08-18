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

### West Haven Funeral Home (West Haven, CT) — serves Orange

`https://www.westhavenfuneral.com/listings` is Cloudflare-protected — confirmed
2026-08-17 via both curl (browser user agent) and WebFetch, both returning a 403
with `server: cloudflare` and a `__cf_bm` cookie. Unlike the Dignity Memorial
sites, WebFetch does not get through here. Note as unreachable; use Cody-White
(above) as the working Orange source.

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

### Robinson Wright & Weymer Funeral Home (Centerbrook/Essex, CT, Dignity Memorial) — serves Chester, Haddam, East Haddam, Killingworth

`https://www.dignitymemorial.com/obituaries?locationcode=3477` gets through
WebFetch fine (same as Fulton-Theroux above — Dignity Memorial URLs are
Cloudflare-protected against curl but not WebFetch), but the returned
summary is unreliable for less-common towns: confirmed 2026-08-18, WebFetch
against this exact URL asked to flag Killingworth returned "None of the
obituaries specifically mention Killingworth, CT," listing roughly 20 of the
50 entries as "Location not specified." A direct WebSearch (`"of
Killingworth" Connecticut obituary 2026`) immediately surfaced a real
Killingworth decedent (Tom Stevens, died Aug. 1, 2026) whose listing was
presumably one of those "not specified" rows — WebFetch's summarization
step is dropping town data it likely has, not the underlying page lacking
it. Don't trust a WebFetch "no mention of [town]" verdict for this locationcode
without cross-checking via WebSearch, especially for towns other than
Essex/Chester/Old Saybrook, which dominate the visible listing.

This locationcode also isn't the only source for Killingworth despite being
the only one `FuneralHomes.csv` listed — Biega Funeral Home (already tracked
above for Durham/Middlefield/East Haddam) turned out to have handled the
Stevens obituary too, so it's now added as a second Killingworth row. Same
caution as the East Haddam case applies: Biega's own in-site search may
still fail to surface a town's obituaries even when they exist on the site
(see the Biega section above), so lead with WebSearch for Killingworth
rather than trusting either site's search box.

Confirmed for Chester too (2026-08-18) — this town fared much better than
Killingworth/East Haddam; WebSearch readily surfaced four real, verifiable
Chester decedents (Thomas F. Miksa, Florence Lewis Broach, James A.
Zanardi, Gail Miller Moorhouse), all explicitly stated "of Chester" in the
obituary body. Same reverse-mislabeling pattern as the Biega/Haddam case
applies here too: individual Dignity Memorial permalinks for Miksa and
Zanardi are both tagged `centerbrook-ct` in the URL (Robinson Wright &
Weymer's own town) despite both obituaries explicitly stating Chester —
don't let the URL slug override the obituary's own stated town.

Also found on the Chester check: the original WebFetch summary of this
locationcode (done for the East Haddam/Killingworth checks) had listed
"Nathan L. Jacobson, 97, Chester, CT, died 07/02/2026" as one of the 50
entries. A dedicated WebSearch for that name turned up no matching 2026
obituary — only a Chester-based civil engineering firm bearing his name
and an unrelated 2023 obituary for a Geraldine Jacobson (his late wife,
apparently). This reads as a WebFetch summarization hallucination, not
just the dropped-town-data issue documented above — treat every entry from
a WebFetch summary of this URL as needing independent WebSearch
confirmation before including it, not just the town field.

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

### Beecher & Bennett Funeral Service (Hamden, CT) — serves Bethany, Woodbridge

`https://www.beecherandbennett.com/obituaries` is a Duda-platform site whose
obituary list is rendered client-side by a Tukios widget (`data-widget-id`
`2d918be729a04a8884cf8c3869e8a4f4`, funeral home tagged `tukios_fhid: "9457"`
in page metadata) — curl and WebFetch both return only the page shell (nav,
empty widget container), no listing data. No public JSON API found behind it
either: the obvious paths under `https://websites.tukios.com/api` (seen
elsewhere on the page powering `/v1/subscriptions` and `/v1/branches`) —
`/v1/obituaries`, `/v1/obits`, `/v1/fh/{fhid}/obituaries`, with `fhid`/`fhId`
as query param — all 302-redirect to `websites.tukios.com/login`; it's an
authenticated admin API, not public data.

It **does** render fine in an actual browser (claude-in-chrome), though —
confirmed 2026-08-17. Navigate, then `wait` ~4s and scroll before reading;
the widget lazy-loads and `get_page_text` right after navigate still shows
the empty shell. Once loaded it's a paginated feed (605 pages at 10/page as
of this check — this looks like a shared multi-funeral-home Tukios feed, not
just Beecher & Bennett's own listings) with a working text search box at the
top (type a query, press Return; URL becomes `?query=...`).

Searching `Bethany` returned **zero results** (confirmed 2026-08-17) —
despite `FuneralHomes.csv` pointing Bethany here as its nearest option, this
feed doesn't currently carry any Bethany-flagged obituaries. This tracks with
two known August 2026 Bethany decedents found via WebSearch instead: Mary
Parcella ran through the New Haven Register/Legacy.com, and Madeline Slicer
through Prospect Memorial (Prospect, CT) — neither via Beecher & Bennett.
Treat this source as low-yield for Bethany specifically; don't assume "zero
results" means no recent Bethany deaths, just that this funeral home isn't
where they're being published. Re-run the search each time rather than
trusting this as a permanent verdict.

Same result for Woodbridge (confirmed 2026-08-18): searching `Woodbridge`
also returned zero results, despite `FuneralHomes.csv` pointing Woodbridge
here too. The four Woodbridge decedents found instead (Salvatore Santo
Petruzzello, Dr. Henry B. Samson, Barbara P. (Wakelee) Glover, Iva "Jeanne"
Russ) were spread across four different funeral homes (Jenkins-King &
Malerba in Ansonia, Robert E. Shure & Son in New Haven, Riverview in
Shelton, William R. McDonald), none of them Beecher & Bennett and no single
one covering enough volume to be worth adding to `FuneralHomes.csv` as a
dedicated Woodbridge row the way Prospect Memorial was for Bethany. Legacy.com
is the more productive route for Woodbridge specifically — go there directly
rather than starting with Beecher & Bennett's search.

Working fallback for Bethany in the meantime: `legacy.com`'s per-town page
(`legacy.com/us/obituaries/local/connecticut/bethany`) and
`prospectmemorialfh.com/listings` both 403 WebFetch/curl directly, but both
render fine via claude-in-chrome (same wait-and-render pattern as above) —
confirmed 2026-08-17, see their own notes below. WebSearch also surfaces
individual Legacy.com obituary pages by name/town directly, and
`echovita.com/us/obituaries/ct/bethany` fetches cleanly via plain WebFetch —
though it lags real publication by several weeks, so cross-check it against
a WebSearch for the requested date range before treating it as current.

### Legacy.com per-town pages (e.g. `legacy.com/us/obituaries/local/connecticut/bethany`)

403s WebFetch/curl but renders fine via claude-in-chrome — navigate, `wait`
~4s, then read. Confirmed 2026-08-17 for the Bethany page: 453 total results,
sorted "Newest" first by default, real listings (Mary M. Parcella, Madeline
Slicer (Razza), Russell Samuel Woodward, ...). Two things to watch for:

- An email-capture modal ("Never miss a notice") pops up on load/scroll —
  close it (X button), don't fill in an email.
- `get_page_text` is unreliable on this page — it picks one `<article>`
  element rather than the full list (returned an entry buried in the results,
  not the top one shown on screen). Use `screenshot` + scroll instead of
  trusting `get_page_text` here.
- Same false-positive risk as the Dignity Memorial/Old Lyme case: this is a
  "local" page, not a strict town filter — one entry (Ann Marie Wilkinson)
  was tagged "1938 - 2026" with body text "of Ansonia CT," not Bethany, despite
  showing up on Bethany's page. Check each entry's stated town, don't trust
  placement on the town page alone.
- The "Search within results" keyword box (left sidebar) is not a reliable
  town filter either — confirmed 2026-08-18 on the Woodbridge page: typing
  `Woodbridge` only trimmed the result count from 1022 to 928 and didn't
  reorder or purge the non-Woodbridge entries already on screen. Don't rely
  on it to do the residence-filtering for you; keep checking each entry's
  stated town manually.
- More false-positive examples, from the Woodbridge page (2026-08-18): a
  decedent "of Madison" who was merely "raised in Woodbridge" (and appeared
  twice, as "Christina Marie Del Santo" and "Christina Marie Del Santos" —
  same person, two newspaper syndications, same photo); a decedent "of
  Westerly, RI"; a decedent who "died... in Boynton Beach, FL" (likely a
  Woodbridge, CT native who'd since relocated); and an entry via "Scott's
  Chapel Hill Mortuary" with no stated CT town at all — Chapel Hill isn't a
  CT place name, so this is likely a same-named town in another state (there
  are Woodbridges in NJ and VA too) that Legacy's aggregation pulled in.
  Exclude anything whose stated residence doesn't clearly read as the CT town
  in question.
- Clicking a result's title/name to open the full obituary was unreliable in
  claude-in-chrome during this session — clicks registered (title underlined
  on hover) but didn't navigate or open a new tracked tab. When you need the
  full text (residence buried past the excerpt's "...", or a canonical URL
  for the Source line), WebSearch for `"Full Name" town obituary` instead —
  it reliably surfaces the direct Legacy.com/funeral-home permalink plus
  enough of the obituary text to confirm residence, without fighting the
  in-page click.

### Prospect Memorial Funeral & Cremation Services (Prospect, CT) — serves Bethany

`https://www.prospectmemorialfh.com/listings` 403s WebFetch/curl but renders
fine via claude-in-chrome — confirmed 2026-08-17 (handled Madeline Slicer,
the Bethany decedent Beecher & Bennett's feed didn't have). The page has a
"Name" vs. "Word" radio-button search plus a month-tab picker (`Aug '26`,
`Jul '26`, `Jun '26`, `<`/`>` to page further back); each result row shows a
town label on the right. Select "Word", type the town, click Search. A
search for `Bethany` correctly surfaced Madeline Slicer and Robert Smith
(both town-labeled "Bethany") but also returned Robert Schlitter, labeled
"Naugatuck" — the search isn't matching only the town label, so verify the
town column on each result rather than trusting the query to have filtered
it. An "Immediate Need" call-us popup also appears on load — close it (X)
before interacting with the search form underneath.

### Biega Funeral Home (Middletown, CT) — serves Durham, Middlefield, East Haddam, Killingworth, Haddam

`https://www.biegafuneralhome.com/obituaries/` is another Tukios-powered
site ("Funeral Home Website by Tukios®" in the footer) — same platform as
Beecher & Bennett, but this one renders promptly via claude-in-chrome with
no extra scroll/wait needed beyond the initial ~4s, and has a plain visible
search box ("Search obituaries") rather than one that only appears after
scrolling. Confirmed 2026-08-18: searching `Durham` returned "No obituaries
found," same zero-result pattern as Beecher & Bennett/Bethany and
Beecher & Bennett/Woodbridge, despite `FuneralHomes.csv` listing Biega as
Durham's (and Middlefield's and East Haddam's) nearest option. Confirmed for
Middlefield and East Haddam too (2026-08-18, same "No obituaries found"
result both times), completing the pattern for all three towns Biega
supposedly serves.

Important caveat found on the East Haddam check: WebSearch turned up a real,
live obituary hosted directly on this site —
`biegafuneralhome.com/obituary/david-weidlich-sr` (David E. Weidlich Sr., 85,
of East Haddam, died January 16, 2026) — that the site's own search didn't
surface for a `East Haddam` query. So the in-site search isn't just "no
current obituaries for this town," it's failing to find obituaries that
demonstrably exist on the site. Don't treat a "No obituaries found" result
here as proof of absence — try a WebSearch site-scoped check
(`site:biegafuneralhome.com "town name"`) before concluding the feed is
truly empty for a given town.

Working fallback: Legacy.com's per-town page again (`legacy.com/us/
obituaries/local/connecticut/durham`, `.../middlefield`, `.../east-haddam`)
— found real, verifiable entries this way for all three towns (Durham:
Marjorie A. Dahlmeyer, James T. McKenna, Andrew T. Szymaszek, Edward Weston
Chapman; Middlefield: Geraldine Emily Zehren, Peter James Ferretti; East
Haddam: George Leon Neudecker Jr., Russ Reid Bochain, Judith Hayes
Beatson), all cross-checked via WebSearch rather than trusting the
Legacy.com excerpt alone, since several of that day's excerpts didn't state
a town within the visible "..." cutoff.

East Haddam's Legacy.com page (2026-08-18) was the worst of the three by
far — most entries on it were upstate-New-York residents (Malone, NY;
Queensbury, NY) with no apparent Connecticut connection, seemingly a data
mixup rather than the usual "different town, same name" false positive.
Given how polluted the page was, WebSearch directly (`"of East Haddam"
Connecticut obituary 2026`) was more efficient than scrolling/verifying
entry by entry. Also worth knowing: Moodus is a village within East Haddam
— an obituary saying "of Moodus" should be treated as an East Haddam match,
not excluded as a different town.

Two more East Haddam sources surfaced this way that aren't in
`FuneralHomes.csv`'s Biega/Dignity Memorial pair: Spencer Funeral Home
(East Hampton, CT — already listed for East Hampton itself) handled Judith
Hayes Beatson, and Aurora-McCarthy Funeral Home (Colchester, CT) handled
George Leon Neudecker Jr. Both added as additional East Haddam rows.

Also confirmed for Killingworth (2026-08-18, WebSearch found a Killingworth
decedent — Tom Stevens — handled by Biega) and Haddam (2026-08-18, three
decedents: Robert "Bob" Duval, Martin S. Ramsey Sr., Rudolph F. Marica) —
both towns now added as Biega rows in `FuneralHomes.csv` alongside their
existing Dignity Memorial listing.

**Reverse false-positive pattern, found on the Haddam check:** Legacy.com's
own page title/breadcrumb for Biega-handled obituaries sometimes tags the
decedent's town as "Middletown, CT" — Biega's own town — rather than the
decedent's actual town of residence. Two of the three confirmed Haddam
entries (Robert Duval, Martin Ramsey Sr.) carried "Middletown, CT" in their
Legacy.com listing title despite the obituary body clearly stating they
lived in Haddam (Duval: three decades on the Haddam Park and Recreation
Commission, Haddam P&Z, Haddam Board of Selectmen; Ramsey: "passed away...
at his home in Haddam"). This is the inverse of the usual false-positive
risk elsewhere in this file (a town's listing page pulling in someone who
doesn't actually live there) — here, a genuine local match can get
mislabeled with the funeral home's town instead. Don't filter out or
deprioritize a Biega-sourced result just because its title says
"Middletown" — read the obituary body for the actual stated residence.

Middlefield's page had a notably high false-positive rate (2026-08-18) —
worth budgeting extra WebSearch verification time for this specific town.
Excluded despite appearing on the Middlefield page: Pamela H. Barna (stated
residence Clinton), Italia "Ty" Giacco (stated residence Middletown/
Cromwell — she's buried in Middlefield, but burial ≠ residence), Valerie L.
Butler and Cheryl Cammarota (a funeral service location and an employer in
Middlefield respectively, but no stated residence there), and J. Michael
Bishop — a Nobel-laureate cancer researcher who died in San Francisco,
swept into the local page's "Notable Deaths" carousel with no connection to
Middlefield at all found. Also checked and found stale: Patch's dedicated
`patch.com/connecticut/durham/obituaries` (labeled "Durham-Middlefield")
does town-tag its entries reliably but hadn't been updated past April 2024
as of this check — don't rely on it for anything called "recent."
