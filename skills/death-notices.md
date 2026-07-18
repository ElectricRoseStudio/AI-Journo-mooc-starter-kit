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
[Full Name], [age], of [Town], died [Month Day]. [One factual sentence: occupation/service/defining detail]. [One sentence: survived by / key family, if available]. Arrangements are being handled by [Funeral Home Name].
```

- Keep each notice to 2-3 sentences — this is a notice, not a full obituary.
- Do not editorialize or add sentiment not present in the source ("beloved," "cherished") unless quoting the source directly.
- Attribute the source implicitly via the "Arrangements are being handled by" line; if asked to publish, note that these are drawn from funeral home listings and not independently verified beyond what the listing states.

## Customization

Add town-specific or style additions below this line:

<!-- Your additions here -->
