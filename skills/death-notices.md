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
