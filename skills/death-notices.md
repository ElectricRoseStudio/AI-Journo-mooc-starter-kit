# Death notices skill

Retrieves recent obituaries from local funeral home listing pages and converts them into short, publishable Death Notices.

## When to use

Invoke this skill when asked to pull obituaries/death notices for a town, e.g. "give me death notices for Waterford."

## Process

1. Look up the requested town in `FuneralHomes.csv` to get its funeral home(s) and obituary listing URL(s). A town may have no funeral home of its own and rely on homes in neighboring towns — the `Notes` column flags this with "No Funeral Home in Town."
2. Fetch each listing URL. Some funeral home sites (e.g. Dignity Memorial properties) block automated fetches (403) or render listings via JS/carousel with no server-side content — note these as unreachable rather than guessing at their contents.
3. From the reachable listings, keep only decedents whose residence matches the requested town (funeral homes often serve multiple towns).
4. Drop any entry missing a usable age or date rather than guessing — note it as skipped for incomplete data.
5. Sort by date of death, most recent first, and take the requested count (default 4-6).
6. Convert each into a Death Notice using the format below, in AP style per this repo's `CLAUDE.md` (no Oxford comma, numerals for ages, spell out other numbers under 10).

## Death Notice format

```
[Full Name], [age], died [Month Day]. [One factual sentence: occupation/service/defining detail]. [Second factual sentence: occupation/service/defining detail] [Third fctual sentence: survived by / key family, if available]. Source: [URL link to obituary]
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

Individual obituary page (for the Source link) follows this pattern:
`https://www.codywhitefuneralservice.com/obituaries/{First}-{Middle}-{Last}?obId={Id}`
(periods stripped from middle initials, spaces in surnames become hyphens). Verify
with a HEAD/GET before using — construct from the `FirstName`/`MiddleName`/`LastName`
fields in the API response, not from `FullName`.

The `DomainId` is specific to Cody-White; if the same platform shows up for another
funeral home (same `tributecenteronline.com`/`site-builder` JS bundle structure),
find its `window.API.domainId` by fetching the home page HTML directly with curl
(not WebFetch) and grepping for `window.API.domainId`.
