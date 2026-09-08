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

### Swan Funeral Homes (Deep River, CT) — serves Deep River

`FuneralHomes.csv`'s URL for this one
(`legacy.com/funeral-homes/connecticut/deep-river/swan-funeral-homes-inc/fh-4189`)
is a genuine dead link — confirmed 2026-08-18 via both WebFetch (404) and
claude-in-chrome (Legacy.com's own "Sorry, this item isn't currently
available... temporarily suspended or deleted" page). Flagged directly in
the CSV's Notes column rather than just here, since a request to fetch it
will fail outright rather than just under-deliver. No replacement URL found
for Swan Funeral Homes specifically — fall back to Legacy.com's town page
(below) instead of trying to relocate this funeral home's own site.

Legacy.com's Deep River page (`legacy.com/us/obituaries/local/connecticut/
deep-river`) was another low-yield one (2026-08-18) — of the first several
entries shown, most turned out to be false positives on verification:
Richard Aronson (actually Danbury, CT), Elizabeth Anne Clark (actually New
Haven, CT), Allison Darling (born St. Johnsbury, VT, handled by a Vermont
funeral home), and Michael Charles Perreault (handled by a Vermont funeral
home, no CT connection found). One entry, Edith Deeg, explicitly said "of
Deep River, Connecticut" in the body text but was handled by a
Massachusetts funeral home (Kelly Funeral Home – Lee) and Massachusetts
newspaper (The Berkshire Eagle) with no independent confirmation
findable — treated as unverifiable rather than trusted at face value,
given how many other entries on this same page turned out to be wrong.

`echovita.com/us/obituaries/ct/deep-river` fetched cleanly via plain
WebFetch (per the general echovita note earlier in this file) and its list
included two names that couldn't be independently confirmed via WebSearch
(Frances E. Confrey, Debra Ziobron) alongside two that could (Mark J.
Gawlak, Vincent Guy Vecchitto). Don't treat echovita inclusion alone as
sufficient confirmation for this town — a name needs to independently
surface via WebSearch (ideally landing on the funeral home's own obituary
page) before counting it as verified.

### Montville — Uncasville and Oakdale are villages within it, not separate towns

Confirmed 2026-08-18: an obituary stating "of Uncasville, CT" or "of
Oakdale, CT" should be treated as a Montville match — both are villages
within the town of Montville (same pattern as Moodus/East Haddam earlier
in this file). Don't exclude a decedent just because the stated town isn't
literally "Montville."

**Woyasz & Son Funeral Service** (`woyaszandson.com/obituaries/obituary-
listings`) has branches in both Norwich and Montville, but its own listing
page skewed heavily toward false positives when checked against Montville
specifically — of five names WebFetch's summary suggested might be
Montville-connected, only one (Robert W. Miles, "of ... Oakdale,
Connecticut") panned out on verification. The other four turned out to be
Norwich residents (Douglas John Caisse Sr., Margot Hacker Gibbs) or had no
stated Montville/Uncasville/Oakdale connection at all despite appearing
on this feed (Lillice Bonita Fissette — grew up in Griswold, died at a
nursing facility in East Lyme; Deborah L. Durand — died at a Plainfield
care facility). This site is also Tukios-powered like Beecher & Bennett
and Biega — obituary text loads client-side and needs `get_page_text`
rather than `screenshot`/scroll, since the page didn't visually scroll to
reveal more body text but `get_page_text` returned the full obituary
regardless.

**Montville Funeral Home of Church & Allen** (Dignity Memorial,
`locationcode=2080`) fared much better, and is worth contrasting with the
locationcode=3477 "Nathan L. Jacobson" hallucination documented in the
Chester section above. Here, the WebFetch summary of the batch listing
reproduced identically (same 50 names, ages, dates, in the same order)
across two different URL forms (`?locationcode=2080` vs. the equivalent
`/obituaries/uncasville-ct` city page) — a strong signal the underlying
data is real rather than fabricated, since a hallucinating summarization
step would be unlikely to reproduce an identical 50-item list verbatim
twice. Individual-permalink verification confirmed several names directly
(Roger S. Kaufmann, James F. Rondeau, Leo J. Plourde) by asking WebFetch to
extract the specific obituary's URL from the listing page, then fetching
that URL on its own — this is the reliable pattern for this locationcode:
don't stop at the batch-listing summary, always pull the individual
permalink and re-fetch it before treating a name as confirmed. One name
from the batch (Thomas Charles Boyle, 33) had a real, live permalink but
the obituary itself was too thin to confirm any town connection — excluded
for insufficient information rather than treated as a false positive.

### Church & Allen Funeral Service (locationcode=2085) and Labenski Funeral Home (locationcode=5089) — serve Norwich

Both locationcodes checked out well on individual-permalink verification
(2026-08-18), same reliable pattern as Uncasville/locationcode=2080 above:
pull each candidate's specific obituary URL from the batch listing, then
fetch that URL on its own rather than trusting the listing summary at
face value. Four of five checked names confirmed cleanly this way (John A.
Majewski Sr., Donald A. Cosentino, Paul E. Daley, Billie Sue Hill).

**New false-positive pattern found here: birthplace, not residence.**
Cathleen Frances Mulcahy was tagged "Norwich, CT" on the batch listing, and
her individual obituary's opening sentence also mentions Norwich — but only
as her birthplace ("She was born in Norwich, Connecticut... to the late
Francis and Rita (Arpin) Coleman"). Reading further in, the obituary states
she "took pride in her Lebanon home, which she maintained with her husband
Michael for 36 years" — she was actually a longtime Lebanon, CT resident.
This is a distinct pattern from the previously documented ones (burial
location ≠ residence, funeral-home town ≠ residence): here it's birthplace
≠ residence, and it can appear in the *opening sentence* of the obituary,
not just buried in the body — don't stop reading after the first town
mention, keep going for a later, more specific residence statement.

Cummings-Gagne Funeral Home (`cummings-gagnefh.com`) — the third Norwich
source in `FuneralHomes.csv` — 403s WebFetch (Cloudflare-protected, same
signature as the other non-Dignity-Memorial/non-Tukios sites documented
elsewhere in this file). No workaround found; rely on the two Dignity
Memorial locationcodes for Norwich instead.

### Dinoto Funeral Home (Mystic, CT) — serves Ledyard

`https://www.dinotofuneral.com/Obituaries.htm` is on an older platform
("Powered by CurrentObituary.Com") — WebFetch returns an empty shell (no
obituary data at all, not even a degraded summary), so this one needs
claude-in-chrome from the start; confirmed 2026-08-18.

Two structural things worth knowing about this specific site:

- Unlike the Dignity Memorial/Tukios feeds elsewhere in this file, this
  page only shows five "current" obituaries at a time — there's no
  50-entry batch listing to page through. A "Search Archive" box (by last
  name, or by month/year in `mm/yyyy` format) exists for older entries but
  wasn't tested this session; worth trying if five current names aren't
  enough for the requested count.
- Clicking a name to open its obituary, then clicking "Obituaries" in the
  sidebar to return to the list, left the page in a state where the
  *next* click didn't register — confirmed repeatedly 2026-08-18. Always
  follow a "return to list" navigation with a throwaway click-and-wait (or
  just re-click the same target once) rather than assuming the first
  click after navigating back will land.

This funeral home serves a wide New London County area, not just
Ledyard, and the "current five" skewed accordingly — of five names shown,
only two turned out to be Ledyard residents (Bernard "Bernie" Lippman,
Calvin Maurice Brown); the other three were Norwich (Mallory Erin Ahern
Young, Paulino Cotto) or Mystic (John "Jack" W. Pillar Jr. — Mystic is a
village within Groton/Stonington, not Ledyard). Each obituary's opening
sentence stated the town plainly this time (e.g., "Calvin Maurice Brown,
age 79, of Ledyard, Connecticut"), so verification here was straightforward
once the page was actually rendered — the difficulty was entirely
mechanical (WebFetch blindness, the click-after-navigate quirk), not
false-positive risk in the text itself.

### Duksa Family Funeral Homes at Newington Memorial — serves Newington

`https://www.newingtonmemorial.com/obituaries/obituary-listings` is
another Tukios-powered site — unlike Beecher & Bennett/Biega/Woyasz,
WebFetch actually returns real listing data here (names, ages, towns,
dates), no browser needed just to see the batch list. Confirmed 2026-08-18.

The listing page has a "Location" filter, but it's a *serving-branch*
filter (Newington Memorial vs. Burritt Hill), not a decedent-town filter —
selecting "Newington Memorial" narrows to obituaries handled by that
branch, not to Newington residents specifically. Don't treat it as a town
filter.

More importantly: individual obituary pages on this site frequently don't
state the decedent's town of residence anywhere in the body text at all —
only funeral/visitation/burial locations, which can be a different town
entirely (the top listing's Mieczyslaw Ledas obituary named a Newington
visitation and a New Britain church/cemetery, with no residence statement
either way). Don't infer residence from which of the two branch locations
(Newington vs. New Britain) hosted the service. For this specific site,
skip straight to a WebSearch for `"of Newington" Connecticut obituary
[timeframe]` rather than trying to confirm residence from the obituary
page itself — every name confirmed this session (Gail R. Sohn, Gary M.
Donovan, Carl Joseph Thiesfield, Andrew John Martin) was verified this way,
via search snippets or other syndication sources that did state "of
Newington" explicitly, not from the newingtonmemorial.com page itself.

One borderline case worth flagging: Charlotte (Downard) Testa's obituary
called her "of Windsor" (her stated current-residence tag) while also
describing her as "a longtime Newington resident" — historical connection,
not current address. Treated as a Windsor resident and excluded, consistent
with the rule of trusting the explicit "of [Town]" tag over other town
mentions in the text.

### Dillon-Baxter and Farley-Sullivan Funeral Homes — serve Wethersfield

Confirmed 2026-08-18: both of `FuneralHomes.csv`'s listed Wethersfield
sources render fine via claude-in-chrome (no WebFetch blindness issue
here), but both turned out to serve a much wider Hartford-area radius than
just Wethersfield. Checked at least 8-10 recent entries across the two
sites via individual permalink; real Wethersfield residents were the
minority — most were Hartford, East Hartford, Bolton, East Lyme, or
Westerly, RI. Dillon-Baxter in particular skewed almost entirely Hartford
proper. Neither site's search box supports filtering by town (Dillon-Baxter
is name-only and returned zero hits for "Wethersfield" as a name search;
Farley-Sullivan has a "Locations" dropdown but it appeared to filter by
serving branch, same pattern as Newington's Duksa site above, not
decedent town).

Given the low hit rate, WebSearch (`"of Wethersfield" Connecticut obituary
[timeframe]`) was more efficient than working through the listing pages —
every confirmed name this session (Robert Parsons, James William Clynch,
Donna Georgina Vergo, Diane Bayek) was found this way, landing on either a
Legacy.com snippet or a different funeral home's page (Newington Memorial,
Dignity Memorial) entirely, not Dillon-Baxter's or Farley-Sullivan's own
site.

### Munson-Lovetere Funeral Home (Woodbury, CT) — serves Woodbury

`https://www.munsonloveterefuneralhome.com/obituaries` explicitly states
on-page that it covers "Woodbury, Southbury, Bethlehem, Middlebury, South
Britain, Oxford, Newtown, Roxbury, Washington, Washington Depot, New
Milford, Bridgewater, Watertown, Sandy Hook" — a wide multi-town service
area, and this shows up directly in the results: of ~8 recent entries
checked individually (2026-08-18), zero were confirmed Woodbury
residents — actual towns included Southbury (service location only, not
necessarily residence), Plymouth, Roxbury, and even Pompton Plains, NJ (a
Cedar Crest Senior Living resident whose burial was scheduled in Newtown,
CT). The site's own search box does support a location string but doesn't
filter meaningfully — searching "Woodbury" returned an unrelated person
literally surnamed Woodbury rather than town matches.

Also confirmed here: a candidate that looked promising from a WebSearch
AI-generated summary (John Gannon, "78... entered into eternal rest...”
with Woodbury mentioned in the search snippet) turned out on direct
fetch of the individual obituary page to have **no stated residence at
all** — only a Southbury church service and a Woodbury burial cemetery
(New North Cemetery). This is the same burial-location-≠-residence trap
documented elsewhere in this file, but notable because it fooled a
WebSearch summary specifically — always pull the individual obituary and
check for an explicit "of [Town]" statement before trusting a search
result's implied town, even when the search engine's own paraphrase
states it as fact.

Working pattern: skip the site's listing page and go straight to
WebSearch (`"of Woodbury" Connecticut obituary [timeframe]`) — this
surfaced three confirmed Woodbury residents (Bette Gurry, Vincent Joseph
Russo, David L. Benjamin), two of which were hosted on
munsonloveterefuneralhome.com itself (findable by direct URL once you
have the name) despite not showing up as Woodbury-relevant on the site's
own front-page listing.

### Biega Funeral Home and Doolittle Funeral Service — serve Middletown

Middletown itself wasn't in `FuneralHomes.csv` until 2026-08-18 — added
with these two funeral homes (both headquartered in Middletown) as
sources, discovered as a side effect of tracking Biega and Doolittle
through other towns' entries earlier in this file.

Biega's search box (documented above as unreliable for filtering by town
for its *other* served towns) worked fine here in the sense that typing
"Middletown" didn't error, but it also didn't visibly filter the
unfiltered ~10-result listing — same non-filtering behavior noted
elsewhere for this site. Individual-entry verification was still
necessary: of 5 checked, only 2 stated Middletown residence explicitly
(Richard A. Hunt, Nicholas J. DiStefano); the rest were Meriden
(birthplace/high school only mentioned Middletown) and Cromwell (birthplace
only).

**Doolittle's per-entry location tag on the listing page is unreliable in
both directions** — confirmed 2026-08-18, a new and more concerning
pattern than the single-direction mislabeling documented elsewhere in this
file:

- Nancy Clough was tagged "Middletown" on the listing page, but her full
  obituary states she and her husband "built a beautiful life in Madison...
  for 35 years" — no Middletown residence claim anywhere in the body.
  Excluded despite the tag.
- Martha Louise (Stocking) Livingston was tagged "Cromwell" on the listing
  page, but her full obituary's opening sentence explicitly states "88, of
  Middletown" — Cromwell was only her birthplace. Included despite the tag
  saying otherwise.

Because the tag can be wrong in either direction, it cannot be trusted at
all for this site — always open the individual obituary and read the
opening sentence for the actual "of [Town]" statement, treating the
listing-page tag as a hint at best, not a filter. This is stricter than
the general rule elsewhere in this file (which mostly warns about listing
pages including entries from *other* towns, i.e. false positives) — here
a real match can also be tagged as excluded (false negative), so don't
skip an entry just because its listing tag doesn't say the target town.

**Checked for a Dignity Memorial source too (2026-08-21): none exists.**
`dignitymemorial.com/obituaries/middletown-ct` 404s — unlike Madison,
Southington, Norwich, etc., Middletown has no dedicated Dignity Memorial
town page. The nearest Dignity Memorial property, Della Vecchia Funeral
Home (Southington, CT, locationcode 8533), lists only Southington and
Wolcott as its current service area; its page mentions Middletown only in
a historical note about a 1987 partnership with the now-defunct D'Angelo
Funeral Home of Middletown, CT. No active Dignity Memorial location claims
Middletown as service area. Biega and Doolittle remain the only sources
for this town — don't add a Dignity Memorial row to `FuneralHomes.csv`
for Middletown without a new location actually opening there.

### Bethel — `hullfuneralservice.com` listing is stale, not just hard to reach

`FuneralHomes.csv`'s only listed Bethel source, "Bethel Funeral Home" at
`https://www.hullfuneralservice.com/listings` (shared with Danbury's Hull
Funeral Home — one site, two branded location tabs), renders fine via
claude-in-chrome — Cloudflare doesn't block a real browser session here,
unlike the pure-curl 403 case for the *other* Cloudflare-protected sites
documented elsewhere in this file (Byles-MacDougall, Impellitteri-Malia,
Neilan). But confirmed 2026-08-19: its newest listed entry is dated January
25, 2025 — over 18 months stale as of this check, despite the funeral home
clearly still operating (its footer copyright says "© 2026"). This is a
different failure mode from access-blocked or unreliable-tag sites
documented elsewhere: the page loads and renders correctly, the town labels
on it look accurate, there's just nothing recent to find. Don't spend time
troubleshooting access to this specific listing if it looks current-day but
returns old dates — treat it as a dead end and go straight to alternate
sources.

### Bouton Funeral Home (Georgetown/Wilton, CT) — serves Redding

`https://www.boutonfuneralhome.com/obituaries` is Tukios-powered (confirmed
2026-08-21 via raw HTML grep for `tukios`) — same platform as Beecher &
Bennett/Biega/Woyasz above. Plain curl/WebFetch returns HTTP 200 but only
the page shell (search box, filters, no listing data); claude-in-chrome
wasn't connected in this session to try the render-and-wait workaround, so
unconfirmed whether that pattern (documented above for the other Tukios
sites) works here too.

Individual obituary permalinks on this site (`boutonfuneralhome.com/
obituaries/{first}-{last}`) do fetch cleanly via plain WebFetch once you
have the name/slug, though — two Redding decedents (Jennifer Sand Bodurtha,
Richard Alan Chudd) were confirmed this way after WebSearch surfaced the
direct permalink.

Better working source for this town: Dignity Memorial's Danbury listing
(`https://www.dignitymemorial.com/obituaries/danbury-ct`) reliably carries
Redding entries (WebFetch returns real listing data, ~50 entries per page,
each already tagged with a town and individual permalink) — four more
Redding decedents (Wade William Roese, Lori Ann Rogers Acosta, Betty Lee
Leota Kent [actually a Jowdy Kane/Legacy.com obituary, not Dignity
Memorial — see below], Rochelle Feinberg) were confirmed this way, each
re-fetched individually rather than trusted from the batch summary alone,
consistent with the Nathan Jacobson hallucination caution documented
elsewhere in this file. All were tagged "Redding, CT" directly (unlike the
Killingworth/East Haddam cases, no "Location not specified" ambiguity hit
for this town).

Betty Lee Leota Kent was found via general WebSearch, not the Dignity
Memorial listing — she was handled by Jowdy Kane Funeral Home (Danbury),
whose own site (`jowdykanefuneralhome.com`) 403s WebFetch (Cloudflare-
protected, same signature as other blocked sites in this file). Her
obituary details were confirmed instead via the Legacy.com syndication
(`legacy.com/us/obituaries/name/betty-kent-obituary?id=62083206`), which
also 403s WebFetch directly but was reconstructed reliably from two
independent WebSearch query results returning identical, detailed
biographical facts (birth date, family names, retirement community) —
treated as confirmed given the consistency, per the same logic as the
Church & Allen locationcode=2080 verbatim-reproduction case above.

Both Bodurtha and Chudd, and several Meadow Ridge entries generally,
resolved to Redding via Meadow Ridge, a retirement community within the
town — worth knowing as a landmark if it recurs.

### East Haven Memorial, Porto Funeral Homes, Clancy-Palumbo — serve East Haven

Confirmed 2026-08-21: East Haven Memorial (`easthavenmemorial.com`) and
Porto Funeral Homes (`portofuneralhomes.net`) are both Cloudflare-protected
— 403 on both curl and WebFetch, same signature as the other
Cloudflare-blocked sites in this file. No workaround tried yet
(claude-in-chrome wasn't connected this session); untested whether a real
browser gets through.

Clancy-Palumbo (`clancy-palumbofuneralhome.com`) is on the same
TributeCenterOnline platform as Cody-White (documented above) —
`window.API.domainId` for this domain is
`ba0c7e03-3ccf-45ea-b597-30cedc3dc8a2`. The same
`GetObituariesExtended` API call works with this DomainId and returned 50
full records directly via curl (no browser needed). Unlike Cody-White,
`PlaceOfResidence` was null but the `Description` field's opening sentence
reliably states residence in plain "`Name age of Town`" form for the
overwhelming majority of entries — this feed was unusually clean, every
East Haven match in this session's batch had an explicit, unambiguous "of
East Haven" (or "of East Haven and [OtherTown]" for dual-residence cases)
in the very first sentence, no burial/birthplace/branch-tag false positives
hit this time.

Individual permalink pattern confirmed:
`clancy-palumbofuneralhome.com/obituaries/{First}-{Middle}-{Last}?obId={Id}`
(same shape as Cody-White's, middle name included when present, e.g.
`Kimberly-Maria-Boucher`, but omitted where the API's `MiddleName` was
blank, e.g. `Andrew-Proto`, `Robert-Palmer`) — all six checked resolved
200 directly, no need to re-derive from a listing-page HTML fetch the way
Cody-White's pattern required.

### Fairfield — added to `FuneralHomes.csv` 2026-08-21

Fairfield had no `FuneralHomes.csv` row at all until this session, despite
having its own funeral homes (unlike the "No Funeral Home in Town" towns
elsewhere in this file). Five were found and added: Spear-Miller, Larson,
Frank Polke & Son, Parente, Daystar Cremation Service.

**Daystar Cremation Service** (`daystarcremation.com/obituaries`, redirects
from `/listings`) is also Tukios-powered (`tukios_fhid: "7887"`) — not
tried via claude-in-chrome, since Spear-Miller alone already yielded a full
6-name batch this session. Untested whether it's the same shared feed as
Spear-Miller or a separate one.

**Larson, Frank Polke & Son, Parente** are all Cloudflare-protected — 403
on both curl and WebFetch, same signature as elsewhere in this file. Not
yet tried via claude-in-chrome.

**Spear-Miller** (`spearmillerfuneralhome.com/obituaries`) is Tukios-powered,
same as Beecher & Bennett/Biega/Woyasz/Bouton above, and needed
claude-in-chrome to render — but needed a *longer* wait than the other
Tukios sites documented in this file: an initial ~4s wait plus
`get_page_text` still showed the empty shell; a further ~6s wait (so ~10s
total) before the listing actually appeared. If a Tukios page comes back
empty after the usual short wait, try waiting longer before concluding
claude-in-chrome can't render it either.

This is a shared multi-town feed like Beecher & Bennett's, not
Spear-Miller-exclusive — despite the page's own text claiming it's "a
collection of obituaries for Fairfield, CT," the first 20 entries checked
included plenty of Westport, Stratford, Shelton, Norwalk, Bridgeport, and
even Lake Ozark, MO residents. Roughly half of entries mentioning
"Fairfield" turned out to be "formerly of Fairfield" (current residence
elsewhere — excluded per the Charlotte Testa/Windsor rule established for
Newington) or birthplace/raised-in mentions (excluded per the Cathleen
Mulcahy/Norwich rule established for Norwich) rather than current
residence. Confirmed current-Fairfield matches this session (Rainville,
Blair, Lasko, Mastronardi, Coscia, Montague) all had an explicit,
unambiguous "of Fairfield" (or equivalent "lifelong Fairfield resident" /
dateline "Fairfield, Connecticut") tied to their *current* residence, not
a past one.

One new false-positive pattern, not previously documented in this file:
**Bruce A. Benway Sr.** was tagged "of Southport and Pompano Beach,
Florida" with the body stating "He resided in Fairfield and later in
Southport" — Southport is a village within the Town of Fairfield (same
landmark pattern as Uncasville/Montville and Moodus/East Haddam elsewhere
in this file), but his *most recent* stated residence reads as split
between Southport and Pompano Beach, FL, with no way to tell which was
current at death. Excluded for ambiguity rather than assumed either way —
worth a second look if Southport-tagged entries recur for Fairfield.

Individual permalink pattern: `spearmillerfuneralhome.com/obituaries/
{first}-{last}`, but nicknames used in the source name sometimes get
folded into the slug (`james-jay-rainville`, not `james-rainville` —
the plain version 302-redirects to a search-results page instead of
404ing, so a bad guess won't obviously fail). Confirm with a curl status
check before trusting a constructed permalink.

### Westport — added to `FuneralHomes.csv` 2026-08-21

Westport had no row until this session. Two sources found and added:
Harding Funeral Home (own listing, physically in Westport) and Lesko
Funeral Home (physically in Fairfield despite branding claiming to serve
Westport — see below).

**Harding** (`hardingfuneral.com/obituaries/obituary-listings`) is on
FrontRunner Professional (per its own footer) and Cloudflare-protected
against curl/WebFetch — 403 both ways, same signature as elsewhere in this
file — but rendered fine via claude-in-chrome, same as the Bethel
`hullfuneralservice.com` case. Unlike that case, individual obituary pages
also 403 WebFetch directly (unlike the Dignity Memorial pattern where
WebFetch gets through even when curl doesn't) — everything on this domain
needs the browser, not just the listing page. The listing itself shows
only name/dates, no town or residence — every candidate had to be opened
individually to check for a stated town.

This listing is *not* purely Westport despite the funeral home's physical
location there: of the first 10 names checked, two were false positives
worth noting as new patterns — **Paula Marie Barta** was actually a
Stoystown, PA resident (obituary cross-posted here likely because she
graduated Staples High School in Westport and is interred in Wilton — a
"once lived here" echo, not current residence), and **Suzanne Solis** was
a longtime Weston resident (not Westport — easy to mis-scan, different
towns) who'd more recently moved to Meadow Ridge in Redding. Both excluded.
The other six checked (Dworkow, Pollak, Englebardt, Schiavone, Lowrie,
Cornette) were clean, explicit current-Westport matches.

Individual permalink pattern:
`hardingfuneral.com/obituaries/{First-Middle-Last}?obId={Id}` — same
FrontRunner/TributeCenter-style shape as Cody-White and Clancy-Palumbo
above, found directly via `read_page` on the listing rather than needing
to guess.

**Lesko Funeral Home** (`leskofuneralhome.com`) markets itself as serving
"Westport, Bridge[port]..." in its page title, but its listed address is
1209 Post Road, Fairfield, CT — a Fairfield funeral home, not a Westport
one. Its obituary listing is also broken/confusing: `/44/Obituaries.html`
redirects to a generic marketing page, which itself redirects to
`/tribute/past-services/index.html`, a JS-driven page that appears to be
serving mismatched branding (its own embedded config referenced a
different company, "Edward Lawrence Funeral Home," with a Darien contact
address) — treat this site's data as unreliable and not worth chasing
further; not used as a source this session.

### W.S. Clancy Memorial Funeral Home (Branford, CT) — already in `FuneralHomes.csv`

Confirmed 2026-08-21: this is also TributeCenterOnline (same platform as
Cody-White and Clancy-Palumbo above) — `domainId`
`9339732b-ee1c-478e-a2e2-42cef7880b9b`. Same `GetObituariesExtended` API
call, no browser needed, 50 records returned directly via curl. Feed was
clean — explicit "of Branford" in the opening sentence for the great
majority of entries, no false-positive patterns hit this session. Main
thing to watch: **North Branford is a separate town**, not a village
within Branford (unlike the Uncasville/Montville or Moodus/East Haddam
cases) — several entries tagged "of North Branford" showed up in the same
feed and were excluded. Individual permalink pattern:
`wsclancy.com/obituaries/{First}-{Middle}-{Last}?obId={Id}`, middle name/
initial included when the API provided one.

### Madison — added to `FuneralHomes.csv` 2026-08-21

Madison had no row until this session. There's a Swan Funeral Home in
Madison itself (825 Boston Post Road), distinct from the Swan Funeral Home
already tracked for Clinton (`locationcode=2632`) — different physical
address, and its Dignity Memorial page uses the town-slug URL form
(`dignitymemorial.com/obituaries/madison-ct`) rather than a locationcode
one; the `/funeral-homes/connecticut/madison/swan-funeral-home/2680` page
403s curl and its locationcode wasn't recoverable that way, so the
town-slug URL is the one recorded. Don't assume same-named Swan Funeral
Home entries elsewhere in this file share a location — check the address.

This locationcode was unusually clean and productive: of the top 7
entries in the batch, 5 were explicit Madison matches, no false positives
hit among the 6 checked and confirmed individually this session (Sonnichsen,
Moran, Ryan, Phillips, Etherington, Dowd) — every one had "Madison,
Connecticut" stated plainly, several were lifelong or 60-year residents.
Unlike the Killingworth/East Haddam/Chester locationcode=3477 case, no
"Location not specified" ambiguity or hallucinated entries surfaced here.

### Easton — Monroe locationcode=7075 is low-yield, same pattern as Killingworth

Confirmed 2026-08-21: of 50 entries in the Spadaccino/Gallagher (Monroe,
Dignity Memorial `locationcode=7075`) batch already in `FuneralHomes.csv`
for Easton, only 2 were tagged Easton, and one of those two —
**Joanne Raymond** — was a false positive on individual verification: her
body text stated she "resided in Redding, Connecticut for the last 60
years," directly contradicting the batch's Easton tag. Same
tag-can-be-wrong caution as the Doolittle/Middletown case, but here the
tag was wrong in the direction of a false positive rather than the
Doolittle case's false negative. Only Edward P. Higgins survived
individual verification from this locationcode.

WebSearch (`"of Easton" Connecticut obituary [timeframe]`) was the
productive path for the rest, same pattern as Killingworth/East Haddam —
surfaced Eleanor Dugan (Adzima Funeral Home — already tracked for Oxford),
Helen "Honey" Biro Scala (handled by Abriola Parkview Funeral Home,
Trumbull, though her permalink resolved on a Legacy/ctpost syndication
rather than Abriola's own site), Molly Aileen Kauffman West (Spear-Miller,
already tracked for Fairfield), and Andrew "Andy" Olschan (Green's Funeral
Home, Danbury), each confirmed via a direct permalink rather than trusted
from the search snippet alone. A search restricted to March–May 2026
turned up nothing further, leaving a real gap in the batch between Feb. 19
and June 23 — worth re-checking on a future run rather than assuming it's
complete.

Both Abriola and Green's added to `FuneralHomes.csv` as new Easton
sources. **Abriola** (`abriola.com`) is on the same old FuneralTech/Legacy
CMS as Lesko (documented under Westport above) — its top-level
`/181/Death-Notices.html` page is just a links page, not the real listing;
the actual listing lives at `/tribute/all-services/index.html` (7,371
entries as of this check) and loads fine via plain curl, no browser
needed. **Green's** (`greensfuneralhome.com`) is Cloudflare-protected —
403 on both curl and WebFetch, and unlike Cody-White/Bouton/Harding this
blocks individual obituary permalinks too, not just the listing page
(confirmed by testing the Olschan permalink directly: 403 both ways).
Only usable this session via a WebSearch snippet that had already
extracted the obituary text; not yet tried via claude-in-chrome.

Two other Danbury-area funeral homes turned out to serve Bethel and have
current 2026 listings, both added to `FuneralHomes.csv`:

- **Cornell Memorial** (documented above for Danbury) has a real physical
  Bethel branch (215 Greenwood Avenue) and tags some listing entries
  "Bethel Funeral Home" — but same as the Danbury case, that's a *branch*
  tag, not a residence indicator. Two Bethel-branch-tagged entries checked
  (Waclaw Murdoch Maliszewski, Christopher David Downey) had **no stated
  residence anywhere in the obituary at all** — only funeral/burial
  locations in Bethel. Excluded both for insufficient information rather
  than assumed-Bethel. Only entries with an explicit "of Bethel" statement
  in the body count.
- **Jowdy Kane Funeral Home** (Danbury) surfaced a confirmed Bethel
  resident (Elfriede Utz) via WebSearch, not previously in
  `FuneralHomes.csv` for any town relevant here — added as a Bethel source.

Working pattern given the primary source's staleness: WebSearch (`"of
Bethel" Connecticut obituary [timeframe]`) was the productive path this
session, landing on Cornell Memorial, Jowdy Kane, and Legacy.com/News-Times
pages rather than the stale Hull/Bethel Funeral Home listing.

### Ellington — both listed sources unreliable, WebSearch was the path

Confirmed 2026-08-21: `FuneralHomes.csv`'s two listed Ellington sources
both failed this session, for two different reasons.

**Burke-Fortin** (`smallandpietrasfuneralhome.com/runtime.php?...`) — the
whole domain returned a genuine 502 Bad Gateway (nginx-level server error,
consistent across retries), not a blocking/rendering issue. Distinct
failure mode from the Cloudflare-403 and Tukios-empty-shell cases
documented elsewhere in this file — this is the site's backend actually
down. Worth a retry on a future run rather than assumed permanently dead,
but don't burn time troubleshooting it in the moment.

**Leete-Stevens** (`leetestevens.com/obituary-listing`) loaded fine via
claude-in-chrome (needed a longer ~10s wait — a spinner is visible in a
screenshot even after `get_page_text` looks empty, same lesson as the
Fairfield/Spear-Miller case: don't conclude "genuinely empty" from a quick
check, verify with a screenshot first). It's a shared 3-branch feed
(Enfield, Somers, Windsor Locks per the page's own footer) — of the 8 most
recent entries checked individually, zero were Ellington residents (all
Stafford Springs, Windsor Locks, Enfield, or East Windsor). This town isn't
one of the three branches, so it reads as a poor fit for Ellington
specifically despite `FuneralHomes.csv` listing it as a Somers-branch
option. Individual permalinks are at `leetestevens.com/memorials/{slug}/
{id}/`, a different URL shape from the FrontRunner
`obituaries.frontrunnerpro.com` API pattern documented elsewhere in this
file — this domain uses a different backend (confirmed: the FrontRunner
API endpoint construction requires a `guid` resolved client-side via a
`dmAPI.getSiteExternalId()` JS call not visible in static HTML, so the
curl-based FrontRunner API approach that works for Adzima/Lester Gee/
Harding doesn't work here without executing JS first).

WebSearch (`"of Ellington" Connecticut obituary [timeframe]`) surfaced
four confirmed decedents instead, each cross-checked individually:
Carmelo "Paul" Arigno and Anna Marie King (both via Samsel & Carmon /
Carmon Community Funeral Homes — `carmonfuneralhome.com`, already tracked
for South Windsor), John J. "Jack" Sullivan (Ladd-Turkington & Carmon,
Vernon — same `carmonfuneralhome.com` platform, not yet in
`FuneralHomes.csv` as its own row), and James Paul Michaud (Holmes-Watkins
Funeral Home, not yet tracked). Two false positives caught in the same
search session: an "Arthur J. Weeden" who read as Ellington in one search
snippet but whose full obituary stated he was actually of Adams,
Massachusetts (a different person, same name — confirmed by fetching the
obituary directly rather than trusting the snippet); and a "Linda Lanz"
whose obituary stated she was a lifelong Stafford Springs resident despite
surfacing in Ellington search results. One lead (Kirk Luthgren, reportedly
69, of Ellington, died July 19) was dropped despite two separate WebSearch
attempts — every result repeated the same paraphrased snippet without ever
citing a specific article URL, so it never cleared the bar for a citable
permalink; worth retrying on a future run in case the underlying page gets
indexed.

### Somers — real Leete-Stevens branch, but still low-yield via the listing widget

Confirmed 2026-08-21: unlike Ellington, Somers Funeral Home is one of
Leete-Stevens' three actual branches (Enfield/Somers/Windsor Locks), but
its entries still don't surface proportionally in the shared listing
widget — of 16 names checked individually across two pages of the most
recent entries (spanning Aug. 18 down to Jul. 26), only one (Joyce M.
Brewer) was a Somers match; the rest split across Enfield, Windsor Locks,
Suffield, Broad Brook, and one Tennessee out-of-state relocation. Same
"don't trust a quick empty check" lesson as Ellington applies doubly
here — the listing widget took two rounds of `wait` (roughly 10s+8s) with
a spinner still visible on the first screenshot before real data appeared.

WebSearch (`"of Somers" Connecticut obituary 2026`) surfaced five more
confirmed decedents efficiently in a single query, all handled by Somers
Funeral Home or Leete-Stevens: Deborah Ann Alejandro, Richard "Dick"
Hutton, Gregory Edgar, Nancy Loubier, George Morris. Two of these
(Loubier, and Joyce Brewer from the widget) had directly fetchable
`leetestevens.com/memorials/{slug}/{id}/` permalinks; the rest only
surfaced via Legacy.com syndication links, which 403 WebFetch as usual —
cross-checked via a second independent WebSearch query per name instead
of trusted from one result alone. **Deborah Ann Alejandro** was the one
exception this session where no specific article permalink could be found
at all despite three separate search attempts (direct name search, an
"obituary legacy.com id" search, and guessing the `leetestevens.com`
slug directly, which 404'd without an ID suffix) — her facts were still
well-corroborated (identical across two independent searches), so she was
kept in the notice, cited to the general Legacy.com Somers town-listing
page rather than a specific article, consistent with the general
last-resort pattern in this file but worth flagging as a genuine gap
rather than a solved case.

### Windsor Locks — best-performing branch on the shared Leete-Stevens feed

Confirmed 2026-08-21, working from the same ~16-entry batch checked for
the Somers and Ellington sessions above: Windsor Locks hit at a much
higher rate than either of the other two towns sharing this feed — 5 of
16 checked entries (Cyril Roy, Arthur Dobosz, Lena DiPoppo, Stephen
Bentley, Patricia Kinsley), all with explicit "of Windsor Locks" or
"lifelong Windsor Locks resident" statements and directly fetchable
`leetestevens.com/memorials/{slug}/{id}/` permalinks — no WebSearch
backfill needed this time, unlike Somers/Ellington. No new access issues
to log; `FuneralHomes.csv`'s existing entry for this town is accurate as
is. Worth remembering for next time: when re-checking this shared feed for
a new town, the batch of individually-verified entries from a prior
Somers/Ellington/Windsor Locks session can often be reused directly rather
than re-fetched.

### Nicholson & Carmon (Suffield, CT) — TributeCenterOnline, same as Cody-White

Confirmed 2026-08-21: `carmonfuneralhome.com` is on the TributeCenterOnline
platform (same as Cody-White, Clancy-Palumbo, W.S. Clancy above) —
`domainId` `19e64ac9-a371-4a92-89d5-c1e01cfc7c6b`. Same
`GetObituariesExtended` API call, no browser needed. Note this is the same
domain used by several *other* Carmon-branded funeral homes already in
`FuneralHomes.csv` (South Windsor, Windsor, Granby, East Granby) — it's a
single wide-serving-area feed covering the whole Hartford-north region
(Suffield, Windsor, East Windsor, Windsor Locks, Ellington, Vernon, Avon,
Bloomfield, Hartford, Tolland, South Windsor, East Granby, and more all
appeared in one 50-entry pull), not Suffield-exclusive. `PlaceOfResidence`
was null as usual; the `Description` opening sentence reliably stated town
in plain "`Name age of Town`" form, with **West Suffield** confirmed as a
village within Suffield (same landmark pattern as Uncasville/Montville
elsewhere in this file) — two of five confirmed Suffield matches were
tagged West Suffield specifically. Individual permalink pattern:
`carmonfuneralhome.com/obituaries/{First}-{Middle}-{Last}?obId={Id}`,
same shape as Cody-White/Clancy-Palumbo/W.S. Clancy.

**Heritage Funeral Home** (`suffieldfuneralhome.com/obituary-listing`,
West Suffield's other listed source) is FrontRunner-branded but its API
call returned empty (`guid=62077:MainSite` via the shared
`runtime/311039` backend) — same JS-resolved-guid problem documented for
Leete-Stevens under Ellington/Somers above (this domain's real guid also
isn't visible in static HTML). Not pursued further this session since
Carmon's feed alone yielded enough Suffield matches; try claude-in-chrome
on a future run if Carmon ever runs dry for this town.

One Suffield match (Rita Porcello Rossi) came from the *Leete-Stevens*
feed instead (documented under Somers/Ellington above), not Carmon or
Heritage — a reminder that a town can have valid sources outside its own
`FuneralHomes.csv` row when a neighboring town's feed happens to carry it.

### East Windsor — third TributeCenterOnline site found via browser JS

Confirmed 2026-08-21: `FuneralHomes.csv`'s listed source
(`eastwindsorfuneralhome.com`) is Cloudflare-protected against curl (403)
but renders fine via claude-in-chrome, same as the Bethel/Harding cases —
and it's also on the TributeCenterOnline platform (third site on this
platform found so far, after Cody-White/Clancy-Palumbo/W.S. Clancy/
Nicholson & Carmon). Its page is branded "East Windsor Community Funeral
Home," not "Bassinger & Dowd" as the CSV previously had it — updated the
row name to match (same URL, so likely a rebrand rather than a different
business).

New technique: since Cloudflare blocked curl entirely (couldn't grep the
static HTML for `window.API.domainId` the way Cody-White/Clancy-Palumbo/
W.S. Clancy were found), the domainId was pulled by running
`window.API.domainId` directly via `javascript_tool` in the already-loaded
claude-in-chrome tab — faster than trying to defeat Cloudflare on the curl
side. Once the API is confirmed with `pageSize=100`, curl works fine for
individual permalinks on *some* TributeCenterOnline domains (Cody-White,
Clancy-Palumbo, W.S. Clancy, Nicholson & Carmon) but this one still 403s
individual obituary permalinks via curl even though the listing API call
itself succeeds — inconsistent Cloudflare configuration per-domain on the
same platform, not a fixed rule. The API-returned data itself (name, age,
dates, full description) is authoritative regardless, so a 403'd permalink
citation is still safe to use since its content was independently
confirmed via the API.

Confirms **Broad Brook** is a village within the town of East Windsor
(five villages total per town records: Broad Brook, Melrose, Scantic,
Warehouse Point, Windsorville) — same landmark pattern as Uncasville/
Montville, Moodus/East Haddam, and West Suffield/Suffield elsewhere in
this file. Two of the six confirmed East Windsor decedents this session
were tagged "of Broad Brook" specifically, and two more came from Nicholson
& Carmon and Leete-Stevens's shared feeds (already tracked for Suffield/
Enfield respectively) rather than East Windsor's own site — added both as
additional East Windsor rows in `FuneralHomes.csv` since they turned out
to be productive sources for this town too.

### Windsor proper — low-yield on Carmon's wide-area feed, plus a branch-name trap

Confirmed 2026-08-21: plain "Windsor" (as opposed to East/South/Locks) was
one of the lower-yield towns checked against the Carmon
(`domainId=19e64ac9-...`) 50-entry feed — only 3 clean matches in the
batch (Jean Bill, Betty Magee, Diane Lubynsky), needing WebSearch to find
a 4th (Henry "Hank" Priore). A regex excluding "East "/"South "/" Locks"
around the word "Windsor" in the opening sentence worked well to avoid
false-triggering on the neighboring towns that dominate this feed.

New technique: the TributeCenterOnline API accepts a `searchTerm` query
param — `GetObituariesExtended?...&searchTerm=Priore&...` returned exactly
the matching record(s) directly by last name, without needing to guess or
scrape a permalink from a WebSearch result. Much faster than the
`pageSize=100` full-batch-then-filter approach when you already have a
specific name from a WebSearch snippet and just need its `Id` to build a
citable permalink.

New false-positive pattern: **Gloria Yvonne Austin** was handled by Carmon
Windsor Funeral Home and initially looked like a Windsor candidate, but
Legacy.com's own syndication tagged her town as "Hartford, CT" (she worked
40+ years at Hartford Hospital and was a member of a Hartford church) —
excluded. Same branch-vs-residence trap as the Doolittle/Middletown case,
here surfacing on a Carmon-family site for the first time.

### Miller-Macari Family Funeral Home (Seymour, CT) — serves Oxford

Confirmed 2026-08-21: also Tukios-powered (same platform as Beecher &
Bennett/Biega/Bouton/Spear-Miller elsewhere in this file), and rendered
fine via claude-in-chrome — but the trigger this time was **scroll**, not
just `wait`. A `get_page_text` right after a 10s wait still showed the
empty shell; only after scrolling down did the listing populate (visible
immediately in a screenshot). Worth trying scroll before concluding a
longer wait alone isn't working, on this platform specifically.

This is a shared multi-town feed (Seymour, Stratford, Shelton, Oxford,
Bridgeport, Southbury all appeared on the first two pages) — 5 of the ~19
names checked across two pages were confirmed Oxford matches (Morton
Mitchell, Donna Barriga, Terence Clark, Gerard Brochu, Mary Ellen
Klembara), each with an explicit "of Oxford" or "resident of Oxford"
statement. Two candidates were excluded: Restituto Paris turned out to be
of Seymour despite appearing in the same recent batch, and Nhan Ly's
obituary stated no town at all ("Services are private," no biographical
text) — excluded for insufficient information rather than assumed either
way. Individual permalinks are at `millermacarifh.com/obituaries/
{first}-{last}` and fetch cleanly via plain WebFetch once the slug is
known (from the listing page or a constructed guess), even though the
listing page itself needs a browser — same split as Cody-White/Bouton/
Harding documented elsewhere in this file.

Adzima's own FrontRunner API (documented in this file's original Oxford
entry) is still returning an empty `data` array as of this session,
unchanged from the 2026-08-15 note — Miller-Macari was sufficient this
time so it wasn't investigated further.

### Granby proper — another low-yield town on Carmon's wide-area feed

Confirmed 2026-08-21: like Windsor, plain "Granby" (as opposed to East
Granby) hit poorly on Carmon's 50-entry feed — only 2 matches, and both
were "formerly of Granby" false positives (current residence elsewhere),
zero clean hits. WebSearch filled in all 4 confirmed decedents instead
(Walter Brown Jr., Clyde Harold Bassett, Eileen Ruth Mufatti, Concetta
Piscitelli), each looked up by name via the API's `searchTerm` param
(documented under Windsor above) to get the authoritative full record
rather than relying on the WebSearch snippet.

Confirms **North Granby** and **West Granby** are villages within the
town of Granby (per Granby's own town-profile page, the town also
contains Five Points, Goodrichville, Hungary, Mechanicsville, and
Pegville as named areas) — same landmark pattern as Broad Brook/East
Windsor and West Suffield/Suffield elsewhere in this file.

Another branch-vs-residence false positive, same shape as Gloria Austin
under Windsor above: **Mary Jean Roy** was handled by Hayes-Huling &
Carmon (the Granby-branch funeral home) and her Celebration of Life was
held at a Granby-adjacent church, but Legacy.com's own listing tagged her
town as "East Granby, CT" — excluded. Two Granby matches this session
(Mufatti, Piscitelli) both died at outside care facilities (Governor's
House in Simsbury; her own home is stated as "in Granby" for Piscitelli)
— worth noting Mufatti's case in particular relied on the obituary body
explicitly stating "of Granby" despite dying in Simsbury, not on a
listing-page tag, consistent with the file's general home-vs-care-facility
caution.

### East Granby — echovita's date can be flat-out wrong, not just stale

Confirmed 2026-08-21: East Granby did much better than Granby proper on
this session's mix of sources — 6 clean matches, 5 of them via Carmon
(Rebecca Hayes, Mary Jean Roy — see the Granby section above for why she's
East Granby not Granby, Thomas Moran, George Vischak, Barbara Rusnock,
Bradford Booker), found through a combination of scanning the cached
50-entry batch directly and WebSearch + API `searchTerm` lookups for
names surfaced by `echovita.com/us/obituaries/ct/east-granby` (which,
unlike the Carmon feed, is organized as a genuine single-town page and
was more efficient to scan than filtering a wide-area feed).

**New echovita caution, stronger than the "lags by weeks" note already in
this file for Deep River:** one echovita-listed entry, Matthew Shultz, was
dated "July 11, 2026" on the list, but his own funeral home's page
(`suffieldfuneralhome.com/memorials/matthew-shultz/...`) stated his actual
date of death as **July 11, 2025** — a wrong year, not just a stale
listing. Caught only because the funeral home handling him (Heritage
Funeral Home, West Suffield) was fetched directly for confirmation rather
than trusting echovita's date at face value. Excluded him from this
session's notices as a result. Reinforces the existing rule in this file:
every echovita name needs independent confirmation before counting it,
and that confirmation should include re-checking the *date*, not just the
town and existence of the person.

### Vernon — added to `FuneralHomes.csv` 2026-08-21

Vernon had no row until this session. Three sources added: Burke-Fortin
(physically in Vernon), Ladd-Turkington & Carmon (physically in Vernon,
already tracked for Ellington), and Holmes-Watkins (Manchester, already
tracked for East Windsor/Ellington).

**Burke-Fortin** (`smallandpietrasfuneralhome.com`) is still returning a
502 as of this session, unchanged from the Ellington check — confirms
this is an ongoing outage, not a one-off, though still worth retrying on
a future run rather than assumed permanently dead.

Six clean matches came from a mix of the already-cached Carmon feed
(Marquis, Pfistner), East Windsor Community Funeral Home's feed (Drenga —
same TributeCenterOnline domain documented under East Windsor above), and
WebSearch-confirmed Holmes-Watkins/Carmon records (Whitham, Iacoboni,
Martell). Two false positives excluded: Diana (Johnson) Bonneau, tagged
"Vernon, CT" by Legacy despite her own obituary stating "of Columbia" —
same branch-vs-residence trap as Gloria Austin/Mary Jean Roy above; and
Althea Wojcik, Legacy-tagged "Vernon, CT" but with no town stated
anywhere in her own obituary body — excluded for insufficient information
rather than trusting the tag alone, consistent with the Nhan Ly precedent.

New data-field anomaly, first time seen in this file: Judith Drenga's API
record had a populated `PlaceOfResidence` field reading "Manchester, CT"
— every other TributeCenterOnline record checked across this file had
this field null. Her obituary's own opening sentence explicitly states
"of Vernon," and her daughter is listed as "of Vernon" too — treated the
body text as authoritative over the structured field, consistent with
this file's general rule of trusting the explicit "of [Town]" statement,
but worth flagging that this field *can* be populated and can disagree
with the text, not just be reliably absent.

### Tolland — added to `FuneralHomes.csv` 2026-08-21

Tolland had no row until this session. Both funeral homes physically in
town, Burke-Fortin and Tolland Memorial, share the
`smallandpietrasfuneralhome.com` domain, which is entirely down (502
confirmed on multiple paths, not just the Burke-Fortin runtime.php URL
checked under Vernon/Ellington) — same ongoing outage documented
elsewhere in this file, added anyway per the usual practice of listing a
town's own funeral homes even when currently broken.

Ladd-Turkington & Carmon (already tracked for Ellington/Vernon) and a new
find, **John F. Tierney Funeral Home** (Manchester), covered the gap. Also
TributeCenterOnline — fourth site on this platform confirmed in this file
(after Cody-White, Clancy-Palumbo/W.S. Clancy/Nicholson & Carmon, East
Windsor Community) — `domainId` `d25d329b-642e-49e9-9a93-e33208a44b20`.
Same API pattern works via curl.

One data point worth remembering: WebFetch on an individual
TributeCenterOnline obituary permalink (as opposed to the batch API call)
sometimes only returns a loading-shell summary ("template for obituary
listings... not present in this excerpt") even when the page returns
HTTP 200 — happened for both the Tierney and Carmon individual pages this
session. The API's `searchTerm` lookup returning the full `Description`
field directly is the reliable path; treat a thin WebFetch summary of an
individual permalink as a cue to re-fetch via the API rather than
concluding the obituary itself is sparse.

### Introvigne Funeral Home (Stafford Springs, CT) — new platform, not yet catalogued

Confirmed 2026-08-21: `introvignefuneralhome.com` is on a platform not
matching any documented in this file (not Tukios, FrontRunner, or
TributeCenterOnline) — modern Tailwind-style CSS, an `f1connect.net`
CDN for photos, and schema.org JSON-LD blocks embedded per obituary page.
Cloudflare-protected against curl (403 on both the listing and individual
pages) but renders fine via claude-in-chrome, listing included, no special
wait/scroll trick needed beyond the usual few seconds.

Pagination is clean and explicit: `?pageNumber=2`, `?pageNumber=3`, etc.,
with a real "Go to page N ... 597" control (597 total pages at 5 entries
each) — much easier to page through deliberately than the Tukios sites'
infinite-scroll-style widgets elsewhere in this file.

Serves a wide multi-town area (Ashford, Storrs, Willington, Somers,
Tolland, Hartford, Middletown, Putnam all appeared in the first 3 pages)
— confirms Stafford Springs is the town's dominant village (also
Staffordville, per the town's own community-structure page) and is what
decedents are tagged with rather than "Stafford" plain.

**Individual tribute pages didn't yield full obituary text this
session** — the page loads a "Tribute Wall" view with a truncated excerpt
and a separate "Obituary" tab, but clicking that tab (confirmed via
claude-in-chrome) didn't visibly swap in the full text before the
session's read. Notices built from name/age/date/town plus whatever
fit in the truncated excerpt; worth trying again on a future run — maybe
a longer wait after the tab click, or the tab content lives at a
sub-path not yet found (e.g. `/obituaries/{slug}/obituary`).

### Willington — added to `FuneralHomes.csv` 2026-08-21

Introvigne (documented under Stafford above) also serves Willington, but
paging through its 597-page feed one 5-entry page at a time was too slow
to be worth it as the primary method here — only 2 of ~20 checked entries
were Willington matches. WebSearch (`"of Willington" Connecticut obituary
2026`) found the rest efficiently in a single query, each cross-checked
individually as usual. Unlike Introvigne's own tribute-wall pages (full
obituary text not reachable this session, see Stafford section), the
WebSearch-surfaced permalinks landed on other platforms (Legacy.com,
abbeycremation.com) that gave full text directly.

### Manchester — added to `FuneralHomes.csv` 2026-08-21

Manchester had no row until this session, despite Tierney (already
tracked for Tolland) being physically located there. Tierney's feed
turned out to be Manchester-dominant — 8 of 50 recent entries were clean
"of Manchester" matches, no WebSearch backfill needed, the best hit rate
of any TributeCenterOnline pull so far in this file. **Holmes-Watkins**
(also physically in Manchester, already tracked for Vernon) added too —
it's Tukios-powered (confirmed via curl grep), not yet tried via browser
for Manchester specifically but likely to work given the Vernon/Tolland
precedent for this same domain.

### Glastonbury — added to `FuneralHomes.csv` 2026-08-21

Glastonbury had no row until this session. **Mulryan Funeral Home**
(physically in Glastonbury) returns a 303 redirect on `/obituaries` that
neither curl (`-L` included) nor WebFetch would follow/resolve — a
distinct failure mode from the Cloudflare-403 and empty-shell cases
documented elsewhere in this file. Not yet tried via claude-in-chrome;
worth a browser check on a future run before assuming it's a dead end.

**Glastonbury Funeral Home** — despite the name, its physical address is
in East Hartford, not Glastonbury (confirmed via its own listing:
"Originally founded... 1970" in East Hartford) — is a Dignity Memorial
property. Its `dignitymemorial.com/obituaries/east-hartford-ct` listing
(a new locationcode not previously tracked in this file) carried
Glastonbury well: 5 of 50 entries, each individually verified rather than
trusted from the batch summary, consistent with the Nathan Jacobson
hallucination caution documented earlier in this file. No false positives
hit this time — all five had explicit current-Glastonbury residence
statements, including one (Keith Henson) tagged with two towns
(Glastonbury and Old Lyme) whose body text confirmed Glastonbury via a
specific church membership rather than leaving it ambiguous.

### Clinton — neighboring-town check 2026-09-07

After an initial Clinton pull from its own Swan Funeral Home listing
(`locationcode=2632`), checked whether funeral homes in Clinton's
neighboring towns (Madison, Killingworth) turn up any additional Clinton
residents. Three sources checked, now added to `FuneralHomes.csv`:

**Swan Funeral Home (Madison)** — already tracked for Madison itself
(`dignitymemorial.com/obituaries/madison-ct`, a different physical
location/listing than Clinton's own per the note added 2026-08-21) — did
turn up one genuine cross-town match: Marlene R. Kistenberg, 87, died
April 18, 2026, explicitly tagged "of Clinton, CT" despite appearing on
Madison's page. Worth checking this page for Clinton whenever doing a
fresh Clinton pull.

**Robinson Wright & Weymer** (Centerbrook/Essex, `locationcode=3477`,
already tracked for Killingworth/Chester/Haddam/East Haddam) and **Biega
Funeral Home** (Middletown, already tracked for Killingworth/Haddam/
Durham/Middlefield/East Haddam) — both checked for Clinton residents,
neither turned up any. Biega's in-site search box (already documented
elsewhere in this file as unreliable for filtering by town) also didn't
filter when tried with "Clinton" as the query — same non-filtering
behavior noted for its other served towns.

**Re-fetching the same Clinton locationcode a second time surfaced three
entries the first pass's page summary had silently dropped**: Rosa Aida
Torres (85, died Aug. 11, 2026), Nicholas A. Guerra (80, died July 25,
2026), and John L. Neri (93, died June 30, 2026) — all clean, explicit
"of Clinton" matches that simply weren't mentioned in the first
WebFetch summary of the same page. This is a distinct failure mode from
the Nathan Jacobson hallucination or the Killingworth/East Haddam
dropped-town-data cases documented earlier in this file: here the
summary dropped whole entries, not just a field. Don't treat one
WebFetch summarization pass of a Dignity Memorial listing as a complete
inventory — a second pass (or explicit request for entries in a
specific date range) can surface real matches the first pass missed.

Also excluded this session: Elaine Josephine Brockett, 88, died June 26,
2026 — tagged Clinton but the obituary gives only name, age, date and
town, no other content (same thin-obituary pattern as Fickett/Koster/
Bofinger documented elsewhere in this file).

Westbrook and Old Saybrook (Clinton's other neighbors) still have no
funeral home of their own tracked in this file. Not added this session —
Clinton's own Swan Funeral Home listing already carries a number of
Westbrook and Old Saybrook residents in the reverse direction (Michael J.
Scherer, Marian Elaine Fitzgerald, Francis Charles Graham, Ralph L. Swain
Sr., all found in the same locationcode=2632 batch), suggesting Swan is
already the shared multi-town source for that side of Clinton's border
and a dedicated Westbrook/Old Saybrook row may not add much for Clinton
specifically.

### Waterford — added to `FuneralHomes.csv` 2026-09-07

Waterford had no row until this session, despite being used as this
skill's own example town in its "When to use" section above and despite
three of its funeral homes (Lester Gee, Byles-MacDougall,
Impellitteri-Malia, plus Thomas L. Neilan & Sons via the East Lyme
sections earlier in this file) already being documented here from
Waterford-adjacent research. All four were finally run end-to-end this
session, now that claude-in-chrome is connected.

**Thomas L. Neilan & Sons** — already tracked for Lyme/East Lyme
elsewhere in this file. Its East Lyme Funeral Home branch listing turned
out to be Waterford-heavy: of the first 10 entries checked (originally
while researching East Lyme), 6 were explicit Waterford residents
(Richard C. Lord, Michael Phillip McBride, Mauro Oscar Zaldivar,
Domenica Croft, George Anthony Joseph) against only 1 confirmed East
Lyme match — this branch serves Waterford more than East Lyme itself
despite the branding.

**Byles-MacDougall** (`byles.com`) — previously documented elsewhere in
this file as Cloudflare-protected with "no JS-API workaround found yet."
Confirmed 2026-09-07: it renders fine via claude-in-chrome, same as the
Neilan/Bethel/Harding pattern already established — the Cloudflare block
was curl/WebFetch-specific all along, just not retried via browser until
now. Its own recent-obituaries page carries a wide New London-area mix
(Groton, Uncasville, Gales Ferry, Niantic, Salem) with Waterford as one
town among several — Elizabeth A. Carp (97, of Waterford) was the one
Waterford match in the first 20 entries checked.

**Impellitteri-Malia** (`impellitterimaliafh.com`) — also previously
flagged as Cloudflare-protected with no workaround. Confirmed 2026-09-07
it renders fine via browser too (the `/obituaries` path itself errors;
use the nav link, which resolves to `/listings`). This one skewed more
productively toward Waterford than Byles-MacDougall: of the first 10
entries, 3 were explicit Waterford matches (Daniel J. Del Grosso, Phyllis
Rossetti, Bernard Joseph Breen) alongside New London, East Lyme, Niantic
and Mystic residents.

**Lester Gee** — re-checked 2026-09-07 via browser; still shows an empty
listing (page shell renders, "Search / Previous / Next" controls present,
zero entries), consistent with the empty-API finding from 2026-08-17.
Kept in `FuneralHomes.csv` with a note rather than dropped, in case that
changes on a future run.

Net effect: Waterford now has three genuinely productive sources
(Neilan's East Lyme branch, Byles-MacDougall, Impellitteri-Malia) despite
all three having been written off earlier in this file as
Cloudflare-blocked dead ends — worth remembering to retry any
"Cloudflare-blocked, no workaround" note in this file via claude-in-chrome
before treating it as permanently unreachable.

**Correction, same session:** `FuneralHomes.csv` already had four
Waterford rows before this check — missed initially because the town
field was stored as `"Waterford "` with a trailing space, so a plain
`grep "^Waterford,"` (and presumably any exact-match lookup) silently
found nothing. Removed the stale duplicate rows and kept the
re-researched ones above; if a town search ever turns up surprisingly
empty for a town that "should" have data, check for this kind of
whitespace/formatting bug before assuming the town was never added.

### Orange — West Haven Funeral Home re-checked 2026-09-07

Previously documented above (Cody-White section) as Cloudflare-blocked
to both curl and WebFetch, unlike the Dignity Memorial pattern. Confirmed
2026-09-07 it renders fine via claude-in-chrome, same as the
Waterford/Bethel/Harding pattern. Its "Word" search box doesn't actually
filter, though (typing a town name and submitting returns the same
unfiltered first page) — same non-filtering behavior documented for
several other funeral home sites in this file. The visible listing (10
most recent, "Page 1 of 375") was almost entirely West Haven residents;
no Orange matches found in what was checked. Cody-White's TributeCenterOnline
API remains the productive Orange source — same session pulled 5 explicit
"of Orange" matches from it directly via curl.

### Durham — Biega's search confirmed broken, two more sources added 2026-09-08

Re-confirmed the 2026-08-18 finding: Biega's in-site search for "Durham"
still returns "No obituaries found," despite real, verifiable Durham
obituaries existing. WebSearch (`"of Durham, CT" obituary [timeframe]`)
remains the working route — pulled five confirmed Durham matches this
way, each cross-checked against its own funeral home or newspaper page.

Two of those five led to funeral homes not yet tracked for Durham, now
added to `FuneralHomes.csv`: **Doolittle Funeral Service** (Middletown —
already tracked for Middletown itself, see the Biega/Doolittle section
earlier in this file) handled James Timothy McKenna, and **North Haven
Funeral Home** handled Sandra C. Johnson. North Haven Funeral Home's own
site renders via browser but its obituary-listing nav item didn't expose
a plain link href to click through (JS-driven dropdown) — only the
individual permalink pattern (`northhavenfuneral.com/obituaries/{First-
Last}?obId={Id}`) was confirmed working; the listing page itself is
recorded in the CSV with that caveat rather than a verified path.

One geographic curiosity: Christopher Angelo Benzi's obituary states he
died in "North Haven, VA" — read as the VA (Veterans Affairs) medical
facility in North Haven, CT, not the state of Virginia, given his Army/
police background and stated Durham residence. Worth remembering if this
phrasing recurs elsewhere in this file — Connecticut has no town spelled
that way, but a few towns host VA facilities that get abbreviated like this.

### Middlefield — same Biega search problem, two more sources added 2026-09-08

Same failure mode as Durham: Biega's in-site search for "Middlefield"
turns up nothing despite real matches existing. Rose Hill Funeral Home
(Middlefield's other listed source) 403s to curl/WebFetch; not retried
via browser this session since WebSearch alone found five clean, well-
documented matches spanning January to July 2026.

Two of those five led to funeral homes not yet tracked for Middlefield,
now added to `FuneralHomes.csv`: **Doolittle Funeral Service**
(Middletown — already tracked for Middletown and now Durham, see
earlier sections) handled Otto Max Zimmerman III, and **John J. Ferry &
Sons Funeral Home** (Meriden) handled Geraldine Emily Zehren. Ferry's
`/obituaries/obituary-listings` path is confirmed correct (loads a real
"Obituary Listings" page via browser) but was empty at check time —
worth rechecking on a future run rather than assuming it's a dead end.

This town was already flagged elsewhere in this file for an unusually
high false-positive rate on Legacy.com's general town page — this
session avoided that page entirely, going straight to individual
funeral-home permalinks for each name instead.

### East Hampton — Spencer Funeral Home's search actually works, 2026-09-08

Unlike most other sites documented in this file, Spencer Funeral Home's
"Word" search genuinely filters by town — searching "East Hampton"
correctly dropped a Glastonbury-tagged and a Colchester/Moodus-tagged
entry from the unfiltered list, and surfaced two more genuine East
Hampton matches (Janet McCann, Cassandra Munson) that weren't among the
first 10 unfiltered results. Worth trying this site's own search first
for future East Hampton pulls rather than going straight to WebSearch.

Even with a working search, several listed entries still needed
individual verification: town tags like "East Hampton/Moodus" or "East
Hampton/New Britain" describe a decedent's history, not necessarily
current residence — Francis Rogers (tagged East Hampton/Moodus) was
actually a St. Petersburg, FL retiree whose daughter lives in East
Hampton; Barry Edmonds (tagged East Hampton/Moodus) currently resided in
Moodus. Also excluded: John Forbes IV, whose own obituary never states
his residence — only his father's ("of East Hampton, Connecticut") is
given.

**Name-collision caution:** a plain WebSearch for `"of East Hampton,
Connecticut" obituary` pulled in several results that were actually
East Hampton, NEW YORK (the Hamptons) — sources like easthamptonstar.com
and 27east.com. East Hampton, CT and East Hampton, NY share a name with
no disambiguating word in typical search snippets; always check the
source domain/state before trusting a "of East Hampton" WebSearch hit.

### Durham — neighboring-town check 2026-09-08

Checked Durham's other neighboring towns (Middletown, Middlefield,
Haddam, Madison, Guilford, Wallingford) for funeral homes carrying
current Durham residents, on top of the Doolittle/North Haven sources
already added. Robinson Wright & Weymer (Haddam's Dignity Memorial
source, locationcode=3477) and Swan Funeral Home (Madison) were both
checked directly against their full batch listings — no Durham matches
in either. Guilford Funeral Home's recent obituaries were all Guilford
residents.

**Wallingford & Yalesville Funeral Homes** (`wallingfordfh.com`) is
worth tracking even though this check came up empty — it explicitly
describes itself as serving "Durham-Middlefield, CT" and has real,
verifiable historical Durham obituaries on file (2008-2022), just none
current as of this check. Same platform/branch-filter pattern as Neilan
and Newington Memorial documented elsewhere in this file (Wallingford
Funeral Home vs. Yalesville Funeral Home branches) — renders fine via
browser, empty shell via curl/WebFetch. Re-check on a future Durham run
rather than writing it off.

Rose Hill Funeral Home (Middlefield's other listed source, already
flagged as 403-to-WebFetch) was also revisited here: it's on the same
FrontRunner Professional platform as Adzima/Harding documented earlier
in this file, but unlike those two, its page HTML doesn't expose an
`ExternalUid`/domainId via a plain curl fetch, so the direct-API
shortcut used for Adzima/Harding doesn't have an obvious path here.
Renders an empty shell via browser too (no data even after a normal
wait) — worth trying the browser-render-and-wait pattern again with a
longer wait, or a claude-in-chrome session, before concluding it's a
dead end.

Net result: no new current Durham matches from this round, but
Wallingford & Yalesville Funeral Homes added to `FuneralHomes.csv` as a
legitimate standing source to recheck later.

### Woodbridge — neighboring-town check 2026-09-08

Checked New Haven and Ansonia funeral homes beyond the four sources
already used for Woodbridge (Robert E. Shure & Son, Jenkins-King &
Malerba, Celentano Funeral Home, The Green Cremation).

**Maresca & Sons Funeral Home** (New Haven) has a "Word" search that
genuinely filters by town — searching "Woodbridge" surfaced real,
verifiable matches (Shirley Prout, Angelo Frischetti, Sara Vecchio,
Theresa DeMattie, Pedro Jurado Pujols, Josephine Antonucci), each with a
plausible full obituary, not just a name-collision false positive. But
the most recent hit was Shirley Prout (July 29, 2025) — nothing current
for 2026 as of this check. Worth rechecking on a future Woodbridge run;
this looks like a real but currently-quiet source rather than a dead end.

**Iovanne Funeral Home, Inc.** (New Haven) — a small single-branch home
(its own "Serving Location" filter has only one option, itself). Only
shows 5 obituaries per page; none of the visible entries were Woodbridge
residents.

**Jenkins-King & Malerba** (Ansonia, already tracked from the original
Woodbridge pull) — re-checked its current 10-page listing; still just
the one Woodbridge match already sent (Salvatore Petruzzello), no
additional ones.

**Robert E. Shure & Son** (New Haven, Martin Ledewitz's funeral home) —
its `/obituaries/obituary-listings` page rendered an empty shell via
browser this session (Tribute Technology platform, same empty-shell
symptom as Wallingford & Yalesville documented in the Durham section
above) — could not re-verify it directly this time. Not necessarily a
dead end; worth a longer wait or repeat attempt on a future run.

### Bethany — neighboring-town check 2026-09-08

Checked Cheshire, Naugatuck and Ansonia funeral homes for current
Bethany residents, beyond the two sources already used (Beecher &
Bennett, Prospect Memorial).

**Wakelee Memorial Funeral Home** (Ansonia) — new source, confirmed
productive: Elizabeth "Beth" Kremmel, 78, died Feb. 24, 2026, of
Bethany. Same Tribute Technology empty-shell symptom as Wallingford/
Shure documented above — the first render attempt showed nothing, but a
second screenshot after a longer wait (~8s total) showed the listing
populated. Its plain "Name" search box is name-only, not a town/word
filter, so individual entries still need opening to check residence;
WebSearch (`"of Bethany, Connecticut" obituary [timeframe]`) found the
Kremmel match faster than paging through the listing would have.

**Alderson-Ford Funeral Homes** (Cheshire/Naugatuck/Waterbury) — has a
working keyword search (`fordfh.com/obituaries?query=...`); searching
"Bethany" returned zero results.

Not fully checked, worth a future look: **Naugatuck Valley Memorial /
Fitzgerald Zembruski** (403s to WebFetch, not retried via browser this
session) and **Connecticut Cremation** — both explicitly list Bethany
among the towns they serve.

### Orange — neighboring-town check 2026-09-08

Checked New Haven funeral homes for current Orange residents, beyond
the two sources already used (Cody-White, West Haven Funeral Home).

**Maresca & Sons Funeral Home** confirmed productive again (same
genuinely-working "Word" search documented in the Woodbridge and
Bethany sections above) — searching "Orange" surfaced one clean, current
match: Rosemary Prete, 95, died Feb. 20, 2026, former CFO of A. Prete
and Son Construction Co. Several other hits on the same search were
excluded on individual verification: Frank Mona (split time with
Naples, FL, died there), Mark Esposito and John Pritchard (both
"formerly of Orange," living elsewhere or tagged that way), Edna
Casolino and Genevieve Baldwin (too old — 2024/2025), plus a couple of
unrelated false positives from the word-match (Gina Raffone, Laurine
Wilson) that didn't actually mention Orange in the visible excerpt.

Note the search UI quirk that cost some time this session: the "Word"
radio button and text field sometimes don't register a click/type on
the first attempt (no visible error, just silently reverts to the
unfiltered Name-search state) — always screenshot to confirm the radio
is actually selected and the query text is in the box before hitting
Search, rather than trusting a single click-type-click batch blindly.

**Celentano Funeral Home** (New Haven) — search submitted via Enter key
didn't filter this time (stayed at "Page 1 of 140," the full unfiltered
count) — likely needs the Search button clicked rather than Return
pressed, consistent with the quirk just noted for Maresca. No Orange
matches in the unfiltered top 10 shown.

### Waterford — neighboring-town check 2026-09-08

Checked Montville and Salem for funeral homes with current Waterford
residents, beyond the four sources already used (all effectively New
London-based: Neilan, Byles-MacDougall, Impellitteri-Malia, Lester Gee).

**Montville Funeral Home of Church & Allen** (Dignity Memorial,
locationcode=2080, already tracked for Montville itself) — pulled and
checked all 50 entries individually rather than trusting a single
summary (consistent with the verbatim-reproduction reliability noted
for this locationcode in the Montville section earlier in this file).
Only one Waterford match: Normand J. Hickey, but he died June 3,
2025 — over a year stale, not usable for a current notice. The feed
is dominated by Uncasville and Oakdale (villages within Montville, not
Waterford). Added to `FuneralHomes.csv` as a checked-but-currently-stale
source, worth rechecking later rather than a permanent dead end.

**Woyasz & Son Funeral Service** (Norwich/Montville) — no Waterford
residents in its current 5-entry listing (mostly Norwich); 403s to
WebFetch, checked via browser instead.

**Salem, CT** — no dedicated Salem funeral home found at all; searches
only surfaced Waterford's own existing sources or unrelated towns.

Net result: no new current Waterford matches this round. Waterford's
existing four New London-area sources remain the productive cluster.

### New Milford — neighboring-town check 2026-09-08

New Milford's other neighbors (Kent, Sherman, New Fairfield,
Bridgewater, Roxbury, Washington, Warren) don't appear to have their
own dedicated funeral homes; Brookfield was the one productive lead.

**Brookfield Funeral Home** (on the Cornell Memorial platform, shared
with Danbury and Bethel locations) explicitly serves New Milford.
Confirmed one current match: Ellen L. Thompson, 67, died Aug. 6, 2026 —
a longtime New Milford Public Library children's librarian. The site's
own `/obituaries/obituary-listings` page rendered an empty shell via
browser even after two separate ~4s waits (same Tribute Technology
symptom as Wallingford, Shure, and Wakelee documented earlier in this
file, but this one didn't resolve even with the longer-wait fix that
worked for Wakelee) — found and confirmed the match via WebSearch and
its Legacy.com syndication instead. Worth retrying the site directly
with claude-in-chrome and an even longer wait on a future run.

Three other current New Milford candidates surfaced by the same search
(Carol Ann Johnson, John Leland Orcutt, Karen A. Fumal) all turned out
to already be handled by Lillis Funeral Home, already tracked for this
town — not new sources.

### Lyme — neighboring-town check 2026-09-08

Checked East Haddam's sources (already tracked for that town, not yet
for Lyme) for current Lyme residents, since Salem — Lyme's other
neighbor — has no dedicated funeral home (confirmed absent during the
Waterford neighboring-town check).

**Robinson Wright & Weymer Funeral Home** (Centerbrook/Essex, Dignity
Memorial locationcode=3477) turned up a genuine, well-documented match:
Catherine Jeanne Spencer, 75, died July 27, 2026, of Lyme — chaired the
French Department at Connecticut College until retiring in 2015, with
degrees from the École Normale Supérieure, the Sorbonne, and a doctorate
in French literature. Added to `FuneralHomes.csv` as a Lyme source too.

**Spencer Funeral Home** (East Hampton, already tracked for East
Haddam) — attempted a "Lyme" search but the click landed on stale UI
coordinates from an earlier session and returned the unfiltered
listing; no Lyme residents were visible in it regardless. Worth a clean
retry (screenshot first, confirm radio selection, per the coordinate-
reliability caution noted in the Orange section above) on a future run.

**Aurora-McCarthy Funeral Home** (Colchester, already tracked for East
Haddam) — 403s to WebFetch, not retried via browser this session.

### East Lyme — CSV gap fixed 2026-09-08

Investigating East Lyme's neighboring towns turned up not a new source
but a process gap: the original East Lyme pull (see the East Lyme
sections earlier in this file, under Lyme/Old Lyme) used Dignity
Memorial's Niantic listing (`dignitymemorial.com/obituaries/niantic-ct`,
associated with Fulton-Theroux Funeral Service) to find 4 of the 5
notices actually sent — Derrie Proctor, Charles Spranklin, Ann Marie
Meek, Marilyn Coleman — but that source was never added to
`FuneralHomes.csv`. Only Neilan was tracked. Fixed now; re-confirmed
the page is still productive (Elizabeth Koster, Patricia Bofinger both
still on it, matching entries already known as thin/excluded).

Also checked Byles-MacDougall (Waterford's own New London source) for
East Lyme: found Ellen Pierson Arcara, 74, died Aug. 20, 2026, but her
obituary states only "of Niantic" throughout with no explicit East
Lyme or Waterford statement — excluded per the standing rule against
trusting the ambiguous "Niantic" tag alone (Niantic spans both towns).

Lesson for this file generally: when a WebFetch/WebSearch-sourced
notice batch works, double check afterward that the source actually
made it into `FuneralHomes.csv` — it's easy for a productive one-off
lookup to get used for an email without ever being recorded as a
tracked source.

### Old Lyme — same CSV gap, fixed 2026-09-08

Same pattern as the East Lyme fix above: Thomas L. Neilan & Sons's East
Lyme branch already produced two confirmed Old Lyme matches (Thomas
Sessa, John "Ron" Hamilton Jr. — sent in the Old Lyme addendum email
during the Waterford/Ledyard research) but was never added to this
row. Fixed now.

Checked two other neighboring-town sources while at it: Robinson
Wright & Weymer's full 50-entry batch had no Old Lyme matches (it
found Catherine Jeanne Spencer, but she's tagged "Lyme," already
recorded there). A WebFetch of Dignity Memorial's Old Saybrook page
(`dignitymemorial.com/obituaries/old-saybrook-ct`) returned content
identical to Fulton-Theroux's already-known Old Lyme dataset rather
than distinct Old Saybrook data — inconsistent with an earlier fetch of
the same URL during the Clinton neighboring-town check, which returned
different, Old Saybrook/Deep River-dominated content. Treat this as an
unverified/possibly-cached result, not a confirmed second Old Lyme
source — worth a clean re-fetch on a future run before trusting it
either way.

### Ridgefield — neighboring-town check 2026-09-08

Checked Danbury's other sources (Dignity Memorial's Danbury page,
Danbury Memorial Funeral Home, Hull Funeral Home) for current Ridgefield
residents, beyond Jowdy Kane, Ridgefield's already-tracked source. None
were productive: Dignity Memorial's Danbury batch had no Ridgefield
matches, Danbury Memorial's listing page rendered completely empty
(genuinely no data, not just slow-loading — waited twice, ~9s total),
and Hull Funeral Home is confirmed still stale (newest entries dated
mid-2024, same finding as the earlier Bethel section in this file).

Every current Ridgefield candidate found via WebSearch (James Laslo,
Paul O'Leary, Linda Marconi Rose, Vera Caporale, Peggy Ann VanDeventer)
traced back to Jowdy Kane. Useful clarification surfaced along the way:
Jowdy Kane operates a dedicated Ridgefield branch — "Kane Funeral
Home," 25 Catoonah St., Ridgefield — distinct from its Danbury address,
though both post to the same `jowdykanefuneralhome.com` site. This
explains why it's so productive for Ridgefield: it's a genuine local
funeral home, not just a nearby one. Noted in `FuneralHomes.csv`; no
new source added since this doesn't change which URL to query.

### Montville — neighboring-town check 2026-09-08

Checked Norwich's other two Dignity Memorial locationcodes (already
tracked for Norwich, not for Montville) for Montville/Uncasville/Oakdale
residents, beyond Woyasz and the Uncasville Church & Allen branch
already used for Montville.

**Church & Allen Funeral Service (Norwich, locationcode=2085)** — note
this is a *different* physical location/locationcode than the Uncasville
Church & Allen branch already tracked for Montville, despite the shared
name — checked all 50 entries individually. One Oakdale match: John
Przybyl, 65, died May 18, 2026 — but genuinely thin (name, age, date of
death, birth date, and cemetery only, no occupation or survivors),
consistent with other thin-obituary exclusions elsewhere in this file.
Added to `FuneralHomes.csv` anyway as a low-yield-but-tracked source.

**Labenski Funeral Home (Norwich, locationcode=5089)** — no Montville,
Uncasville, or Oakdale residents at all in its 50-entry batch.

Not checked further: Salem and Bozrah have no dedicated funeral home of
their own (consistent with the pattern found in other neighboring-town
checks this session), and Ledyard's Dinoto was already established as
low-yield with too small a "current" window to be worth rechecking here.

### East Hampton — neighboring-town check 2026-09-08

Checked Portland and Colchester for current East Hampton residents,
beyond Spencer Funeral Home, East Hampton's own source.

**Portland Memorial Funeral Home** (already tracked for Portland) — a
small feed (URL pattern `portlandmemorialfuneralhome.com/{month}-{year}`,
e.g. `/august-2026`); its September listing page didn't render via
claude-in-chrome (the browser extension dropped mid-session and needed
reconnecting — worth remembering that a stalled `browser_batch` result
can mean the extension disconnected, not that the page itself failed),
but the August page fetched fine via WebFetch and had only 3 entries,
none East Hampton.

**Aurora McCarthy Funeral Home** (Colchester, already tracked for East
Haddam) — checked its unfiltered listing (183 pages) and attempted a
"East Hampton" Word search, which didn't filter (same non-functional-
search pattern seen at several other sites in this file); no East
Hampton residents in the visible batch, which skews Colchester/
Marlborough/Columbia.

**New source found via WebSearch: Abbey Cremation Service** (Rocky
Hill) — confirmed one match, Kenneth "Ken" Fox, 73, died July 6, 2026,
of East Hampton. Notably, this is the same 511 Brook Street, Rocky Hill
address as Brooklawn Funeral Home (checked and found unproductive
during the Newington neighboring-town check) — likely two brand names
sharing one physical operation, similar to the Duksa/Newington-New
Britain dual-branch pattern documented elsewhere in this file. Abbey
Cremation also turned out to be the source for Carol Ann Young, one of
the Newington notices sent earlier — added to `FuneralHomes.csv` for
both towns.

### Portland — neighboring-town check 2026-09-08, Doolittle/Cromwell added

Checked East Hampton and Cromwell for current Portland residents,
beyond Portland Memorial Funeral Home, Portland's own (small, low-
volume) source.

**Doolittle Funeral Service, Inc. / Cromwell Funeral Home** (already
tracked for Middletown, physically two locations sharing one site —
same dual-branch pattern as Newington/New Britain and Ridgefield/
Danbury documented elsewhere in this file) turned out to be genuinely
productive for Portland: its "Word" search actually filters correctly,
and surfaced four confirmed current matches — James Edward Piatti (71,
died July 13, 2026, 48-year Portland resident and volunteer fireman),
Louise (Hertzberg) Feldman (87, died June 13, 2026, four decades in
Portland's Gildersleeve section), Elisa (Betances) Jimenez (70, died
May 25, 2026), and Benjamin Austin Adams (34, died April 20, 2026).
Added to `FuneralHomes.csv` for Portland.

One candidate was excluded: Sanda Marie Nickols, tagged "Portland" on
the listing, but her full obituary shows she lived in Marlborough and
then Union Hall, Virginia, only returning to Connecticut in 2017 with
no clear statement of resettling in Portland specifically — the
"Portland" tag likely traces to Portland Care and Rehabilitation
Centre, where she was staying, not a stated home residence.

**Spencer Funeral Home** (East Hampton, already confirmed to have a
genuinely working search) — searched "Portland," returned "No matching
records were found." Clean negative result, not a search failure.

Also worth noting: the Chrome extension disconnected mid-session twice
during this check (once needing `tabs_context_mcp` to reconnect, once
just failing silently on a `browser_batch` call) — when a browser
action returns nothing or the whole batch errors with no per-step
output, check connection status before concluding the page itself is
broken.

### Norwich — CSV gap fixed 2026-09-08

Checked Norwich's neighboring towns (Preston, Bozrah, Griswold, Lisbon)
for their own dedicated funeral homes — none exist; all route back to
Norwich-area homes already documented in this file.

Found the actual gap instead: **Woyasz & Son Funeral Service** is
already tracked for Montville, but it has a dedicated Norwich branch
(141 Central Ave.) and its current listing is actually Norwich-heavy,
not Montville-heavy — yet it was never added as a Norwich source.
Checked its five current entries individually: Thomas Anthony Slaga
(died Sept. 1, 2026, explicit "of Norwich"), Douglas John Caisse Sr.
(68, died Aug. 12, 2026, explicit "of Norwich"), and Tina Ysordia (died
Aug. 22, 2026, "prior to moving to Norwich, she lived in New York
City," implying Norwich as her subsequent/current home) all confirmed.
Two were excluded: Vincent Brophy has deep lifelong Norwich ties (born
there, career at the Norwich Post Office, Norwich Elks Club) but no
explicit statement of current residence anywhere in the obituary; and
Wladyslawa "Virginia" Miroszka states no town at all. Added Woyasz to
`FuneralHomes.csv` for Norwich.

### Chester — neighboring-town check, no new source, 2026-09-08

Checked Biega Funeral Home (Middletown): its search box turned out to
be a literal name search, not a town filter — searching "Chester"
returned only unrelated people literally named Chester (2010-2013).
Checked Doolittle Funeral Service / Cromwell Funeral Home: no Chester
matches. Chester remains fully served by the already-tracked Robinson
Wright & Weymer (`locationcode=3477`); the Swan Funeral Homes (Deep
River) link remains a confirmed 404. Nothing added.

### Essex — neighboring-town check, no new source, 2026-09-08

Checked Essex's neighbors (Old Saybrook, Deep River, Chester,
Westbrook). Essex's own tracked source, Robinson Wright & Weymer, is
physically located in Essex/Centerbrook and already surfaces
Essex-tagged obituaries directly (Steven E. Bancroft, Richard A.
Windatt, Barbara Venable, Marguerite d'Aprile Quigley, confirmed in the
current 50-entry batch). Checked the Swan Funeral Home group listing
(`dignitymemorial.com/obituaries?groupcode=swan`, covering Clinton,
Madison, and Old Saybrook branches) for any Essex tags — none found.
Nothing added.

### Deep River — CSV gap fixed 2026-09-08

Investigating Deep River's neighbors turned up a gap rather than a new
source: **Robinson Wright & Weymer Funeral Home** (`locationcode=3477`)
was already tracked for Chester, Haddam, Killingworth and Essex, but
never for Deep River — even though its current listing plainly
contains Deep River residents. Re-pulled the full 50-entry batch and
confirmed four explicit Deep River tags: Joel Patrick Pierce, Joyce
Calamari Hines (tagged both Deep River and Chester), Marcos Gonzalez,
and Frances E. Confrey. Added Deep River to `FuneralHomes.csv` for this
source. Deep River otherwise still has no working dedicated source —
Swan Funeral Homes remains a confirmed 404.

A WebSearch result surfaced a "Gilberg-Hartwig Funeral Home" in
connection with a Deep River obituary (Sharon A. Howe) — checked and
it's a real funeral home, but located in New Bremen, Ohio, unrelated to
Connecticut. Search-summary artifact, not a lead; discarded.

### Rocky Hill — CSV gap fixed 2026-09-08

Checked Rocky Hill's neighbors (Cromwell, Wethersfield, Newington).
Found another CSV gap: **Doolittle Funeral Service / Cromwell Funeral
Home**, already tracked for Portland/Durham/Middlefield, has a current
Rocky Hill match — Gray Wade Abbott, 62, of Rocky Hill, died Aug. 31,
2026, an Army veteran buried at Colonel Raymond F. Gates Memorial
Cemetery in Rocky Hill. Added Rocky Hill to `FuneralHomes.csv` for this
source.

Also checked: **Abbey Cremation Service**, already tracked for
Newington/East Hampton, is itself physically located in Rocky Hill
(511 Brook Street) but had zero current Rocky Hill-tagged obituaries in
its own listing — no row added despite the local address. **Farley
Sullivan Funeral Home** (Wethersfield/Glastonbury, Tribute Technology)
rendered as an empty shell even after a 6-second wait — same known
platform issue documented elsewhere in this file, flagged for a future
retry rather than a confirmed dead end. **Dillon-Baxter** (Wethersfield,
Tukios) has a search box labeled "Enter Name" — confirmed name-only,
same limitation already seen on other Tukios sites (Biega) — and its
1,551-record archive is too large to page through by hand, so it
wasn't pursued further.

### Rocky Hill — Brooklawn Funeral Home confirmed productive, 2026-09-08

While sending this week's Rocky Hill notices, checked **Brooklawn
Funeral Home** (511 Brook Street, Rocky Hill) — already listed in
`FuneralHomes.csv` for Rocky Hill but never previously used for a send
— and found it highly productive: six current Rocky Hill matches in a
single 20-entry batch (Donald Lewis Langevin, Jacqueline Yolanda
Matthews, Trevor Roy Cameron, Sotiraq Bakri, Reno Libera, Sally R.
Marinelli), each individually verified with an explicit "of Rocky
Hill" statement. Its obituary pages are Cloudflare-protected against
WebFetch (403) like several other Tukios sites this session — use the
browser tools instead. Updated its `FuneralHomes.csv` URL to the direct
`/obituaries` listing page and added a confirmation note.

### Haddam — CSV gap fixed 2026-09-08

Checked Haddam's neighbors (Middletown, Durham, Chester, East Haddam,
Killingworth) and found another CSV gap: **Doolittle Funeral Service**
(Middletown), already tracked for Durham, Middlefield, Portland and
Rocky Hill, was never added for Haddam despite having two confirmed
current matches. Dr. Kenneth L. Eckhart Jr., 85, is tagged "of Haddam"
directly. Marjorie Birdsey Bengtson, 78, is tagged "of Higganum" —
applying this file's standing village-within-a-town rule, Higganum is
a village of Haddam (she also worked for the Haddam-Killingworth
School District, reinforcing the tie). Added Haddam to
`FuneralHomes.csv` for this source. Haddam's existing sources
(Robinson Wright & Weymer, Biega) were unaffected.

### East Haddam — Spencer Funeral Home confirmed productive, 2026-09-08

Checked East Haddam's neighbors (Haddam, East Hampton, Colchester,
Lyme, Salem) and its own already-tracked sources. **Spencer Funeral
Home** (East Hampton, already listed for East Haddam but never
confirmed) turned out to be productive: Robert "Bob" Gendron, 69, is
tagged "of East Haddam" directly; Zdzislaw "Stan" Nastalczyk, 73, is
tagged "of Moodus," which — applying the same village-within-a-town
rule used for Higganum/Haddam — is a village of East Haddam. Added a
confirmation note to its existing row. One candidate on the same page,
Francis Edward Rogers Jr., was excluded: he lives in St. Petersburg,
FL and was only staying at Chestelm Health and Rehab in Moodus, a
facility, not a stated residence.

Checked but unproductive: **Aurora-McCarthy Funeral Home** (Colchester,
also already tracked for East Haddam) had no current East
Haddam/Moodus match — its one Moodus mention, Raymond Churchill Jr.,
is explicitly "formerly of Moodus," now living in Surprise, AZ.
Robinson Wright & Weymer and Doolittle (checked during the Haddam
investigation) had no East Haddam matches either.

### Woodbury — neighboring-town check, no new source, 2026-09-08

Checked Woodbury's neighbors (Southbury, Middlebury, Watertown).
**Carpino Funeral Home** (Southbury, already tracked) and **Brookside
Memorial** (Middlebury, already tracked) both remain confirmed empty
shells — same Tribute Technology rendering issue seen elsewhere, still
unresolved even after a longer wait and, for Brookside, clicking "View
All Obituaries." **Casey Funeral Homes** (Waterbury/Oakville) markets
itself as serving Woodbury among many towns, but its search box is
confirmed name-only, not town-filtering: searching "Smith" surfaced a
result whose body text happened to mention "of Woodbury," but
searching "Woodbury" or "Watertown" directly returned zero results —
and its archive (477 pages) is too large to browse by hand. **Hickcox
Funeral Home** (Watertown, new to this file) was checked directly; no
Woodbury tags in the first page of 1,497 records. Nothing added.

### Middlebury — CSV gap fixed 2026-09-08

While investigating Middlebury's neighbors, found that **Munson-Lovetere
Funeral Home**, already tracked for Woodbury and Southbury, has
confirmed current-year Middlebury matches never captured for this
town: Bruce Alan Meier, 87, "of Middlebury," died April 1, 2026; and
Ryan J. Anderson, 22, "in his home in Middlebury," died April 16, 2026.
Both individually verified. Added Middlebury to `FuneralHomes.csv` for
this source. Also updated Middlebury's existing Brookside Memorial row
with a note confirming the empty-shell issue found during the Woodbury
check above.

### Wethersfield — neighboring-town gap fixed 2026-09-08

Checked Wethersfield's neighbors (Rocky Hill, Newington). Found two
CSV gaps at once: **Brooklawn Funeral Home** and **Abbey Cremation
Service** (both Rocky Hill, already tracked for Rocky Hill and for
Newington/East Hampton respectively) each turned out to have a current
Wethersfield match never captured for this town. Richard "Snooky"
Dalfino Jr., 64, "of Wethersfield, CT," died Sept. 1, 2026 (Brooklawn).
Margarita Matias Sanchez, 69, "of Wethersfield, CT," died Aug. 4, 2026
(Abbey Cremation). Both individually verified via their permalinks.
Added Wethersfield to `FuneralHomes.csv` for both sources.

Also confirmed Wethersfield's own **Dillon-Baxter Funeral Home** is
productive directly: Donald S. Brookman, 67, "of Wethersfield, CT,"
died July 24, 2026. Wethersfield's other tracked source, **Farley
Sullivan Funeral Home**, remains a confirmed empty shell (Tribute
Technology rendering issue) even after a longer wait — noted on its
row for a future retry.

### Danbury — neighboring-town check, no new source, 2026-09-08

Checked Honan Funeral Home (Newtown) — confirmed empty shell, no
content even after an extended wait. Checked Lillis Funeral Home (New
Milford) directly — two entries (Paul E. Martin Jr., Manny Velez Jr.)
mention Danbury only as a birthplace, with New Milford as the actual
residence; excluded per this file's birthplace-vs-residence rule.
Danbury's other neighbors (Bethel, Ridgefield, Brookfield) are already
served by shared branches of Danbury's own tracked sources (Cornell
Memorial, Jowdy Kane). Nothing added.

### Bethel — neighboring-town check, no new source, 2026-09-08

Checked the Dignity Memorial Danbury-area aggregate listing — no
Bethel-tagged entries in the current 50-entry batch. Checked Bouton
Funeral Home (Georgetown/Wilton, already tracked for Redding) — no
Bethel match on its first page; its search box is confirmed name-only
("Search obituaries by name"), and its archive (53 pages) is too large
to browse by hand. Honan Funeral Home (Newtown) remains an empty
shell. Nothing added.

### Brookfield — Cornell Memorial workaround confirmed, 2026-09-08

Checked Brookfield's neighbors: Hull Funeral Home's listing (shared
Danbury/Bethel branches) is stale — every entry dates to 2024, and its
one Brookfield mention (Michael Spagnolo "of Brookfield") is a
grandchild's town, not the deceased's. Honan Funeral Home (Newtown)
remains an empty shell.

The real finding was on Brookfield's own source: Cornell Memorial's
listing page is a confirmed empty shell (Tribute Technology), same as
already documented for its shared New Milford row, but the same
Legacy.com/WebSearch workaround used there also works for Brookfield.
Found and verified John William Lucas, 78, of Brookfield, died Aug.
28, 2026 (published to Legacy.com Sept. 6, 2026 — a retired Brookfield
police detective sergeant and later Danbury Hospital director of
security). Added a confirmation note to Brookfield's own row pointing
to this workaround.

### Monroe — neighboring-town check, no new source, 2026-09-08

Checked Abriola Parkview Funeral Home (Trumbull) — individually
verified all 10 current listings; none tagged Monroe (Trumbull,
Bridgeport, Shelton, Stratford and Milford residents instead). Checked
Green's Funeral Home (Danbury) via search — no Monroe match; Easton
sits between the two towns, making it a geographic stretch. Monroe's
own tracked source, Spadaccino and Leo P. Gallagher & Son, remains
highly productive on its own (several current matches going back to
April). Nothing added.

### Newtown — CSV gap fixed 2026-09-08

Checked Newtown's neighbors and found that the **Dignity Memorial
Danbury-area listing** — already tracked for Redding but never for
Newtown — has four confirmed current Newtown matches: Marion M.
Millard, 91, died Aug. 19, 2026; Frank J. LaPak Jr., 92, died July 21,
2026; Helen Ventura, 82, died July 2, 2026; and Lucille M. Gioello, 91,
"of Maplewood, Newtown, CT," died June 27, 2026 — Maplewood is a
senior living community in Newtown, same pattern as the Essex Meadows
precedent. All four individually verified via their permalinks. Added
Newtown to `FuneralHomes.csv` for this source. Also confirmed Honan
Funeral Home (Newtown's only other tracked source) remains a confirmed
empty shell and added a note to its row.
