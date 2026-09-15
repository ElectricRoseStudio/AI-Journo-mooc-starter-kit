# Recently-sold skill

Pulls the most recent home sales for a given town and writes a Patch-style
"Here Are The Most Recent Property Sales In [Town]" article, narrative format,
citing Zillow.com with the required hyperlinks.

## When to use

Invoke this skill when asked to write a recent-property-sales article for a
town, e.g. "run recently-sold for Ridgefield" or "write the property sales
piece for New Canaan."

## Process

1. **First, look for a CSV in the `recently-sold/` folder at the repo root.**
   Filenames follow the pattern `<TOWN NAME> zillow-properties-sold-<timestamp
   YYYY-MM-DD_HH_MM_SS>.csv` (town name upper-case, e.g. `RIDGEFIELD
   zillow-properties-sold-2026-09-15_12_14_53.csv`). Match case-insensitively
   and on town name only — ignore the timestamp suffix, and if more than one
   file matches a town, use the most recent timestamp. This is a Zillow
   property-export CSV (someone pulls it from Zillow's own export/download
   feature, sidestepping the anti-scraping block entirely) and is the
   **preferred, ground-truth data source** — it eliminates essentially all of
   the verification work described in steps 2-4 below, since every field
   needed for the article is already in the row.

   Columns (confirmed schema, Sept. 2026): `Property URL, MLS ID, Property
   price (USD), Sold date (MM/DD/YYYY), Living area, Living area unit, Price
   per living area unit (USD), Lot/land area, Lot/land area unit, Bedrooms,
   Bathrooms, Street address, City, Zip, State, Country`.

   Sort rows by `Sold date` descending and take the five most recent. No
   further filtering has been necessary so far — the export has already come
   back as clean single-family/condo arm's-length sales — but sanity-check
   for obvious non-residential or $0/placeholder rows before using a row.

2. **If no matching CSV exists for the requested town, fall back to live
   lookup** — and expect it to be much harder:
   - **Do not trust Zillow.com directly for scraping.** `zillow.com/<town>-ct/sold/`
     and individual `zillow.com/homedetails/...` pages return HTTP 403 to both
     WebFetch and curl. Same for Redfin and Trulia detail pages. Don't spend
     time retrying these.
   - **WebSearch's AI-synthesized answers are unreliable for this data and
     will cross-contaminate between properties.** Confirmed repeatedly: a
     price belonging to one address attributed to a different address; an
     address reported "sold" on a date when its own detail page showed it
     was still an active listing; the same address's sale date/price
     changing between two different search phrasings. For a New Milford run
     with no CSV available, 3 of 4 secondary candidates failed independent
     re-verification this way — condo-heavy streets with many similarly
     numbered units seem especially prone to this. **Never take a single
     WebSearch synthesized answer as ground truth for address+price+date;
     always re-verify each candidate with a second, independently-phrased
     search before using it.**
   - **Try the town's official land/property transfer records as ground
     truth** for address, recorded date, and price. Many CT towns publish a
     monthly Town Clerk "transfer report" PDF (worked cleanly for
     Ridgefield, see the Customization section below) — but not all towns
     do; New Milford's land records sit behind a RecordHub portal with no
     free public PDF, and its town assessor's Vision Government Solutions
     "Sales Search" form (`gis.vgsi.com/<townct>/Sales.aspx`) could be
     reached and a real date-range query constructed and POSTed with curl,
     but the server rejected it with a "viewstate MAC validation failed"
     error — an infrastructure issue (load-balancer/server-farm key
     mismatch) outside what a scripted request can work around. Don't sink
     much time into a form-POST workaround if the first attempt fails this
     way; it's a server-side config problem, not a request-shape problem.
   - If a town has no working official-records source and WebSearch data
     won't independently re-verify, **stop and tell the user** rather than
     publish an article built on contaminated data — see the New Milford
     incident in project memory for the exact framing used.
   - Once address+date+price are pinned down from an authoritative source,
     get square footage, bedrooms and bathrooms via a **separate,
     narrowly-scoped WebSearch per address** (e.g. `"239 Keeler Dr"
     Ridgefield CT bedrooms bathrooms square feet`) — much less prone to
     cross-contamination once there's only one real answer to converge on.
3. Take the five most recent qualifying sales (or fewer if a town is
   genuinely low-yield — same pattern as the death-notices skill: report
   what's verified, don't pad the count with anything unconfirmed).
4. Write the article per the format and style rules below.

## Article format

Headline: `Here Are The Most Recent Property Sales In [Town]`

**Opening line, before the stat line — this needs actual flair, not a
filler sentence.** Pattern (confirmed against Patch's own exemplars, e.g.
"Whether it's a sprawling colonial on Avon Drive or a tucked-away condo
near the lake, Orange has been keeping its closing attorneys busy" and
"From a sprawling hilltop estate on Matson Ridge to a cozy Rogers Lake
retreat, Old Lyme's real estate market has been keeping closing attorneys
busy"): one sentence, contrasting two *specific, real* properties from this
batch by street name and a vivid one-or-two-word descriptor (a condo vs. an
estate, a cottage vs. a colonial), closing on a light, town-appropriate
kicker phrase. Pull the contrast and kicker from what's actually
distinctive in that town's batch (a shared street name, a lopsided
price spread, a coastal vs. rural feel) rather than reusing the same
"keeping closing attorneys busy" line every time — a generic "Here's a
look at the five most recent closings" sentence is exactly what this line
should never be.

Bold lede line (comes after the flair line): `There were [N] homes sold in
[Town] between [date] and [date], with a top price of $[X], according to
Zillow.com` (hyperlink Zillow.com per the style rule below).

One narrative paragraph per property, in descending order by sale date:
- Lead with the address (hyperlinked, see below) and sale date and price
- One to three factual sentences of description: type of home, year built,
  neighborhood, notable features — pulled from real listing detail, not
  invented
- **No per-property speculation about what the sale means for the market.**
  Save all interpretation for the closing kicker.

Closing section, always titled exactly `What this says about the [Town]
market:` — a short, synthesizing paragraph looking across all the sales
together (price range, mix of property types, etc.). This is the only place
market interpretation belongs.

End with a brief sourcing note: when the data came from a `recently-sold/`
CSV, a one-line credit is enough (e.g. "Sourced from a Zillow property-sales
export for [Town]."); when it came from the live-lookup fallback, disclose
the Zillow-access workaround and list anything excluded and why (builds
trust, mirrors the sourcing footnote used in the death-notices skill).

## Style rules

- AP style per this repo's `CLAUDE.md`: no Oxford comma, spell out numbers
  under 10 (bedroom/bathroom counts: "four-bedroom, four-bath"), figures for
  measurements/money/square footage
- Abbreviate Jan., Feb., Aug., Sept., Oct., Nov., Dec. only when paired with
  a specific date (spell out when used alone or with only a year)
- Abbreviate Ave., Blvd., St. only with numbered addresses; spell out every
  other street suffix (Drive, Road, Way, Lane, Court, etc.)
- Numbered street names: spell out First through Ninth, use figures for 10th
  and higher
- Hyperlink each property address to its Google Maps page
  (`https://www.google.com/maps/search/?api=1&query=<address+town+state,
  URL-encoded>`); the **printed/visible link text must omit the town name**
  — just the street address — even though the underlying Maps URL should
  include town and state for accurate geocoding
- Hyperlink `https://www.zillow.com/homes/` every time the text "Zillow.com"
  is printed
- Include for every listing: sale date, overall size (square footage), sale
  price, bedroom count, bathroom count

## Delivery

When asked to send the finished article by email, follow the same
disposable-script send mechanism established for death notices
(per-town/per-article throwaway Python script, `smtplib` direct, creds from
`~/.config/newtown-mail.env`, `From: "Patch_Edit_AI <rich@electricrose.net>"`,
append a line to `beat-archive/send-log.txt`) with one difference: **send as
HTML (`MIMEText(html_body, "html")`), not plain text**, since this article's
format depends on working hyperlinks (Google Maps + Zillow.com), unlike the
plain-text death notices. Subject line convention:
`[Town], CT recent property sales — [description]`.

---

## Customization

Add town-specific or platform-specific findings below this line as this
skill gets used more:

### Nine-town batch, Sept. 2026 (Danbury, Newtown, Bethel, Brookfield,
Monroe, Southbury, Redding, Weston, Easton)

More village/neighboring-town `City` mismatches confirmed, same discipline
as the six-town batch: Newtown's CSV mixed in 13 Sandy Hook rows (Sandy
Hook is a village within Newtown — kept, and called out by name in the
article since two of the five picks were Sandy Hook sales, including the
batch's top price); Weston's CSV had one Wilton row (a separate town —
excluded); Easton's CSV had one Fairfield row (a separate town — excluded).
Always run the `City` column check before trusting a filename, every time,
not just the first run.

One Danbury condo (`5 Nabby Road`) had a blank `Bedrooms` field rather than
`0` — read as a studio unit and described in prose as "a studio condo,"
not "a zero-bedroom" or a fabricated number. Check for blank/empty
Bedrooms fields on very small (under ~500 sqft) condo units before either
skipping the row or guessing a count.

When a request lists more towns than are actually distinct (this batch's
request named "Monroe" twice, effectively asking for 9 unique towns while
saying "10 in total"), just write one article per unique town and note the
discrepancy in the summary back to the user — don't ask, and don't write a
duplicate article to hit a stated count.

### Seven-town batch, Sept. 2026 (Vernon, Tolland, Manchester, Middletown,
Glastonbury, Stafford, Willington)

More village-vs-neighboring-town `City` calls, same discipline as before —
kept as legitimate village rows: "Vernon Rockville" (Vernon), "South
Glastonbury" (Glastonbury), "Stafford Springs" (Stafford — this is actually
the *majority* tag for Stafford, not a minority village row, so don't be
surprised when the "main" town name is the minority City value). Excluded
as genuinely different towns: "Enfield" and a stray "Vernon Rockville" row
from Tolland's file, "Ellington" from Willington's file.

**New failure mode, Stafford's file only: rows with a real price but a
blank/zero `Living area` and blank `Bedrooms`/`Bathrooms`.** Three rows
($320,000, $55,000, $97,000) had this shape, each on a large multi-acre
lot — these read as vacant land sales, not home sales, recorded in the
same "sold" export. Filter these out before taking the top five (`Living
area` not empty and not `0`), the same way undisclosed-address rows get
filtered. Don't assume every row in a "properties sold" export is a home.

When two sales in the same batch tie exactly on price (Redding's $1.65M
tie last batch, Middletown's $510,000 tie this one), say so explicitly
("tied for the top price," "matching that price") rather than arbitrarily
picking one as "the" top price — and double-check any kicker sentence that
compares "the two priciest" against "the two [something else]" doesn't
silently assume they're the same two rows. One draft this round claimed
Glastonbury's two highest-*priced* homes were also its two highest
per-square-foot outliers; they weren't (the second-priciest home was
actually inside the tight cluster) — caught and rewritten before sending,
but worth double-checking that kind of claim by explicitly reading off
the sorted numbers rather than assuming a pattern lines up.

### Four-town batch, Sept. 2026 (Fairfield, Westport, Bridgeport, Madison)

"Southport" confirmed (again) as a village within Fairfield — kept, though
none of this run's top five happened to be Southport rows.

Two more ratio/comparison errors caught in kicker paragraphs before
sending, same category as the Glastonbury one above — a real pattern, not
a one-off, so treat any kicker sentence with a specific multiplier ("double,"
"two and a half times," "more than X% above") as needing arithmetic
verification, not just a plausibility check:
- A Fairfield draft claimed the top per-square-foot sale was "nearly two
  and a half times" the next-highest rate; the actual ratio was about
  1.5x. Rewritten to "more than 50% above," which was true.
- A Madison draft claimed a home's per-square-foot rate was "more than
  double" every other sale's; one comparison was actually just under 2x
  (1.97x). Rewritten to "roughly double or more" to stay accurate across
  every comparison, not just most of them.

General rule going forward: before printing any specific multiplier in a
kicker, actually divide the two numbers rather than eyeballing it from the
sorted list.

### Seven-town batch, Sept. 2026 (Southington, Hartford, Farmington, Avon,
Simsbury, Canton, Berlin)

More villages confirmed and kept: Plantsville (Southington), Unionville
(Farmington), Weatogue/West Simsbury/Tariffville (Simsbury — a town can
have more than one legitimate village tag at once), Collinsville (Canton),
East Berlin (Berlin). Excluded as genuinely different towns: a Farmington
row from Hartford's file; Avon and Middletown rows from Farmington's file;
Newington, Monroe and Winsted rows from Berlin's file.

**Hartford's file included multi-unit properties** — rows like "98-100
Capen Street" and "30-32 Wayland Street," an address range rather than a
single number, with 8 bedrooms and 3-4 baths across 4,000+ square feet.
These read as small multi-family/income properties, not single-family
homes with an unusually large bedroom count. Report the bed/bath/sqft
figures as given rather than assuming a data error, but don't describe
them as "homes" the way a single-family sale gets described — call them
"properties" instead, and let the address-range and bedroom count imply
the multi-unit nature rather than asserting it outright (the export
doesn't have a property-type field to confirm it).

Caught two more kicker multiplier errors this round (Farmington's "nearly
doubled" was really ~1.45-1.49x; Simsbury's "more than double the square
footage" was really ~1.6x) — same failure as the four-town batch above.
Switched to absolute differences ("over 1,900 square feet more") instead
of a multiplier in the Simsbury case, which sidesteps the arithmetic risk
entirely when the ratio isn't a clean, obviously-true number. Prefer that
approach over a multiplier unless the ratio is unambiguous (2x+, verified
by actual division) — this is now the third batch in a row with at least
one caught-before-sending multiplier error, so treat every such claim as
guilty until verified by division, not just the more "dramatic" ones.

### Five-town batch, Sept. 2026 (Norwalk, Trumbull, Naugatuck, Shelton,
New Canaan)

**Broaden the verification rule beyond multipliers: any superlative claim
("the lowest," "the highest," "the smallest home," "the cheapest") needs
the same sort-and-check discipline, not just ratio claims.** Two errors
caught in this batch were plain ranking mistakes, no arithmetic involved:
a Naugatuck draft called one sale "the lowest per-square-foot rate of the
five" when a different sale in the same batch was actually lower; a New
Canaan draft called one home both "the lowest per-square-foot rate" and
called a different home "the smallest home," when a third property in the
batch was actually both. Before calling anything the lowest/highest/
smallest/largest in a kicker or a per-property paragraph, write out the
full sorted list for that metric and confirm the claimed property is
actually at the end you're claiming — don't rely on memory of "roughly
where it fell" while drafting each paragraph independently.

Norwalk's file had a legitimate extreme outlier worth flagging as a
pattern: a Rowayton Avenue sale at $2,241/sq ft, nearly 4x the next-highest
rate in the batch (Rowayton is a known high-end waterfront neighborhood
within Norwalk). Don't assume an outlier this large is a data error —
verify it reads as internally consistent (price ÷ sqft matches the stated
rate) and, if so, report it as the real news it is rather than suppressing
or hedging it.

<!-- Your additions here -->

### Ridgefield — first run, Aug. 2026 data

Confirmed working: `ridgefieldct.gov/departments/town_clerk/property_transfers.php`
lists monthly PDF transfer reports going back to 2020 (47 documents as of
Sept. 2026). No September report existed yet when checked Sept. 14 (county
recording lags the actual closing by roughly one to two weeks), so "most
recent" sales pulled from the August report were themselves already two to
three weeks old relative to the request date — mention this lag if a user
asks why the sales aren't from the last few days.

Five sales used: 239 Keeler Drive ($1,350,000, Aug. 31), 58 Olcott Way
($295,000, Aug. 31, a one-bedroom condo — don't be surprised by a low
outlier price among otherwise $1M+ single-family sales, verify it's a condo
unit before assuming it's an error), 2 Gino's Way ($1,940,000, Aug. 26), 59
Rising Ridge Road ($1,526,000, Aug. 26), 19 Kellogg St. ($1,750,000, Aug.
25). Excluded from the same batch: a $0 trust transfer at 27 Woodchuck Lane
and an $185,000 commercial office-unit sale at 90 Grove St. Ste 01.

### `recently-sold/` CSV workflow — first run, Sept. 2026 (six towns at once)

The CSV path (see Process step 1) makes this whole skill dramatically
faster and more reliable than either the WebSearch or land-records fallback
— every field needed is already in the row, no per-address re-verification
needed. But **always check the `City` column value for every row before
trusting the filename** — three separate problems turned up across six CSVs
checked in one batch:

- **The file named `ORANGE zillow-properties-sold-....csv` contained zero
  Orange, CT rows** — every row's `City` was Waterford or Quaker Hill (a
  village within Waterford), a wholly different town on the opposite side
  of the state from Orange. This reads like the wrong export was saved
  under the wrong filename, not a data-quality nuance to route around —
  don't substitute a neighboring town or silently drop the request; stop
  and tell the user the file doesn't match its own filename.
- **`BETHANY`'s file had 40 Bethany rows and 1 Woodbridge row** — filter to
  the requested town's exact `City` value before taking the top five, same
  as the death-notices skill's neighboring-town discipline.
- **`LYME`'s file mixed 36 `Lyme` rows and 5 `Old Lyme` rows** — Lyme and
  Old Lyme are distinct towns (separate Patch beats), not a village
  relationship. Filter to `City == "Lyme"` exactly for a Lyme request;
  don't include Old Lyme rows unless asked for "the Lymes" specifically.
- **`NEW MILFORD`'s file had 4 rows tagged `Gaylordsville`** — unlike the
  Lyme/Old Lyme case, Gaylordsville genuinely is a village within the Town
  of New Milford (same landmark pattern as Moodus/East Haddam in the
  death-notices skill), so these rows are legitimate and were kept.
- One `NEW MILFORD` row had `Street address` literally `(undisclosed
  Address)` — Zillow withholds the exact address on some sales. Skip these
  rows entirely rather than trying to build a Google Maps link for them;
  take the next-most-recent qualifying row instead.

Also note: the CSV's own `Street address` field sometimes pre-abbreviates
suffixes Zillow's way (e.g. `446 Hamburg Rd`, `301 Grove St`) — reformat
per this skill's style rules (spell out Road; abbreviate St. only since
it's a numbered address) rather than reproducing the CSV's own
abbreviation choices verbatim.

**Resolution on the mislabeled "ORANGE" file**: the user confirmed the data
was correct for Waterford, just saved under the wrong filename — so it got
rewritten as a Waterford article rather than fixed/re-exported. If this
happens again, don't assume a mismatched file is always disposable; ask
first, the way this run did before rewriting it, since sometimes the fix
is "it's the right data, wrong label" rather than "get a new export."
