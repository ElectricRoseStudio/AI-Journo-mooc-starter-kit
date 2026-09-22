# Arts-roundup skill

Writes a Patch-style hyperlocal "Weekend Arts Roundup" for a town: a
bulleted list of arts events, exhibitions, films, performances and
family/recreation attractions opening or on view the coming Thursday
through Sunday, with real, verified venue links and Google Maps links.

## When to use

Invoke this skill when asked to write a weekend arts/entertainment
round-up for a town, e.g. "write the arts roundup for New Milford" or "do
an arts-roundup for Clinton this weekend."

A request naming multiple towns joined with hyphens (e.g.
"Lyme-Old Lyme-East Lyme") is a single combined regional article covering
all of them together, not one article per town — see the address-format
note below for how that changes the venue-address rule. A request listing
several towns separated by commas/"and" is one article per town.

## Process

1. **Establish the target window**: "this coming Thursday through Sunday"
   relative to today's date. If today itself is Thursday-Sunday, use next
   week's occurrence unless the user says otherwise — confirm with the
   user if genuinely ambiguous rather than guessing.
2. **Research real, current events for that town** across these categories:
   films, exhibitions (museums, galleries, libraries), performances
   (theater, concerts, historical-society programs), and recreation/family
   attractions (farm fall-fun/corn mazes, fairs and festivals, outdoor
   seasonal attractions). Recreation items are a fallback category, not a
   default one — lead with film/exhibition/performance whenever the town
   has them, and only reach for recreation to round out a thin week or
   when a town genuinely has nothing in the other three categories (see
   the Monroe case in Customization, where recreation was the entire
   article). Useful source types, roughly in order of reliability:
   - The venue's own events page, fetched directly (WebFetch) — this is
     the most reliable source for date/time/title accuracy.
   - The town's Patch events calendar
     (`patch.com/connecticut/<town>/calendar`) — note that Patch cross-
     lists regional events from neighboring towns onto a given town's
     calendar; always check the venue's own address to see whether an
     event is actually *in* the target town or just listed there.
   - WebSearch as a discovery tool only, never as final confirmation —
     see the verification rule below.
3. **Verify every fact before printing it — this is the highest-risk step
   in this skill.** Confirmed repeatedly (see Customization): WebSearch's
   AI-synthesized summaries and small-model WebFetch summaries both
   hallucinate specific details that look perfectly plausible — a
   fabricated event permalink URL that 404s, a day-of-week that doesn't
   match the actual calendar date, a date that conflicts between two
   fetches of the supposedly same event. Rules to follow:
   - Never trust a single search snippet for a date, time, address, or
     URL if it can be cross-checked with a second, independent fetch.
   - **Always verify a URL by actually fetching it before printing it as
     a link.** A URL a summarizing tool "reports" seeing is a claim, not
     a fact, until a direct fetch confirms it resolves (not a 404) and
     shows the expected content.
   - Cross-check computed day-of-week against the actual calendar date
     when a source states both — a mismatch is a strong signal the
     source is unreliable for that entry.
   - If a fact can't be verified and can't be dropped without leaving the
     article thin, say so explicitly to the user rather than publishing
     an unverified guess (same standard as the death-notices and
     recently-sold skills).
4. **Finding a link when there is no calendar permalink.** Patch doesn't
   always give a calendar-listed event its own permalink page (some
   entries only render as an item on the general calendar-list page,
   with no distinct URL). When that happens, don't link the entry to the
   generic calendar page — go to the organizing venue's own website and
   find that specific event's own listing page there instead (e.g. an
   art guild's own "49th Annual Juried Exhibition" page on its own
   domain). Verify that page by fetching it directly per the rule above
   before using it, the same as any other link.
5. Pick a spread of entries across the window — ideally at least one per
   day (Thursday, Friday, Saturday, Sunday) — favoring variety across
   film/exhibition/performance/recreation categories over a longer list
   of similar items. It's fine to have fewer than a "full" list if the
   town genuinely has a light week; don't pad with borderline or
   out-of-window items to hit a target count.
6. Write the article per the format and style rules below.

**A note on running multiple towns as parallel forked subagents**:
WebSearch has a per-session call budget, and that budget is **shared
across every fork in the session, not allocated separately per fork**.
Confirmed across two batches (Sept. 2026): running 5+ towns in parallel
exhausted the shared budget (200 calls) partway through several forks'
research, and the towns whose forks ran later or needed more searches
came back with noticeably thinner, less-verified articles — not because
those towns were actually quiet, but because their fork ran out of
budget before finishing. If a town's finished article looks thin relative
to its size/population, and it was part of a large parallel batch,
suspect budget exhaustion before concluding it was a genuinely light
week — re-run it alone (with a full budget) to check. Consider running
large batches (6+ towns) in smaller waves rather than all at once if
thoroughness matters more than speed.

## Article format

SEO headline, no longer than 109 characters, naming the town and the
draw (e.g. "Ridgefield's Weekend Lineup: Live Music, Theater and Art Fill
Thursday Through Sunday"). **Capitalize the first letter of every word in
the headline**, including short words like "and," "of," "to" and "in" —
initial caps throughout, not standard AP headline case (which would lowercase
those).

Meta description, up to 136 characters, summarizing the article's focus.

One engaging intro paragraph (fun, inviting, accessible tone — no
emoticons) setting up the weekend before the list.

A bulleted list, one entry per event, each including:
- The event/show/exhibition title
- Day(s) and time(s)
- One to two sentences of real, sourced description/flavor — not
  invented detail
- The venue name, hyperlinked to the venue's own official website
- The venue's address, hyperlinked to its Google Maps page — see the
  address-format rule below for what text is visible vs. what goes in
  the underlying URL

Closing paragraph, inviting readers to get out and explore, tying back to
the town's specific mix of events (not a generic "there's something for
everyone" line — reference what's actually in this batch, the way the
recently-sold skill's flair-line rule works).

## Style rules

- AP style per this repo's `CLAUDE.md`: no Oxford comma, spell out numbers
  under 10, dollar amounts as `$45` (not `$45.00`), avoid the listed
  AI-isms and "blasted"
- Times as `8 p.m.`, not `8:00 PM`; dates as `Sept. 26` (abbreviated only
  when paired with a specific day number)
- **Venue address format**: the visible/printed address text must NOT
  include the state or ZIP code. Include the town name in the visible
  text only when the venue is located outside the article's target town.
  - For a single-town article: a venue inside the target town shows
    street address only (e.g. "80 East Ridge Road"); a venue outside it
    shows street + town, no state/ZIP (e.g. "25 Cross Hwy., Redding").
  - For a combined multi-town article (e.g. "Lyme-Old Lyme-East Lyme"),
    there is no single "the town" to omit — show the specific town name
    for every venue among the combined set too, so the reader knows
    which of the towns each event is in; still omit state/ZIP always.
  - The underlying Google Maps URL should always use the full address
    (street, town, state, ZIP) for accurate geocoding regardless of what
    the visible text shows.
- Hyperlink each venue's Google Maps address using
  `https://www.google.com/maps/search/?api=1&query=<full address,
  URL-encoded>`
- Never fabricate or guess a URL. Every link in the finished article must
  have been fetched and confirmed to resolve during research.

## Delivery

When asked to send the finished article by email, follow the same
disposable-script send mechanism used by the recently-sold and
death-notices skills: a per-article throwaway Python script using
`smtplib` directly, credentials sourced from `~/.config/newtown-mail.env`,
`From: "Patch_Edit_AI <rich@electricrose.net>"`, sent as HTML
(`MIMEText(html_body, "html")`, since this article's format depends on
working hyperlinks), and a line appended to `beat-archive/send-log.txt`.
Subject line convention: `[Town] arts & entertainment weekend round-up —
draft article`.

---

## Customization

Add town-specific or platform-specific findings below this line as this
skill gets used more:

<!-- Your additions here -->

### Ridgefield — first run, Sept. 2026

Confirmed working sources: the venue's own events page (Ridgefield
Playhouse's `/events/` page gave clean, accurate listings on direct
fetch) was more reliable than WebSearch for date/time accuracy. The
Ridgefield Patch calendar (`patch.com/connecticut/ridgefield/calendar`)
cross-lists real events from neighboring towns (Redding, New Canaan) —
one of these (a Redding artisan fair) was still worth including, flagged
in the copy as "a short drive from Ridgefield," but its Google Maps
address and venue link point to its real Redding location.

**Caught a fabricated URL before sending**: an intermediate WebFetch
summary reported a specific `ridgefieldlibrary.org/event/...` permalink
for an Olga Fermin art exhibit; a direct fetch of that exact URL
returned a 404. The real page turned out to live on the library's
underlying events platform domain (`ridgefieldlibrary.librarymarket.com`),
found by fetching the library's own events-listing page and reading the
actual `href` out of the HTML rather than trusting a paraphrase of it.
**Always fetch a reported URL directly before using it — a summarizing
tool describing a URL is not the same as that URL existing.**

Also caught: a WebFetch summary of the Patch calendar reported the same
recurring "Oktoberfest" event's date as both Sept. 26 and Sept. 27 across
two separate fetches, and separately reported it as present, then absent,
on the Redding Patch calendar. Treated as unreliable and dropped from the
article rather than guessing which date was correct.

When Patch's calendar didn't give a distinct permalink for two exhibition
entries (a guild's annual juried show, a library art exhibit), found and
verified working permalinks on each organizer's own site instead
(`rgoa.org/49th-annual-juried-exhibition/` and the library's
`librarymarket.com` event page) — this is what step 4 of Process above
generalizes from.

### Five-town batch, Sept. 2026 (New Milford, Clinton, Waterford, Orange,
Lyme-Old Lyme-East Lyme) — run in parallel via forked subagents

All five sent successfully. This batch produced several more confirmed
instances of the same failure mode the Ridgefield run first caught —
**a search snippet or secondary listing describing a fact is not the same
as that fact being true** — worth treating as the norm for this skill, not
the exception:

- **A "same name, wrong place" false positive can hide in a venue name
  itself, not just in a date/URL.** Waterford: "Waterford Gallery of Art"
  (waterfordgalleryofart.com) is in Waterford, *Ireland*, not Waterford,
  CT — only caught because the venue's own page was fetched directly and
  showed an Irish address. Clinton: a "Clinton Historical Society Wine &
  Cheese Fundraiser" turned out, on fetching its own calendar, to belong
  to a same-named historical society in Clinton, NY. **Always confirm a
  venue's own site states the correct state/region, not just the correct
  name, before treating a match as real** — a plausible-sounding local
  org name is not enough on its own.
- **A WebSearch-reported date can be off by more than a year, not just a
  day.** Clinton: WebSearch reported a Danielle Nicole show at The Kate
  (Old Saybrook) as "Sept. 26, 2026," but the venue's own event page
  (`thekate.org/event/danielle-nicole/`) showed April 29, **2025** — a
  stale/cached result surfacing as current. Direct-fetch verification
  caught it before it shipped.
- **A search snippet can describe an exhibition a venue's own page never
  mentions at all.** New Milford: a "Prismic" gallery show was claimed
  for Village Center for the Arts by a search result, but a direct fetch
  of that venue's own gallery page made no mention of it — dropped
  rather than trusted on the snippet alone.
- **When Patch's stated venue address conflicts with the organizing
  group's own site, trust the organizer's site.** The Lyme-Old
  Lyme-East Lyme run found East Lyme's Patch calendar listing a farmers
  market at "Smith's Acres LLC, 4 W Main St," while the market's own
  organizer site (nianticmainstreet.org) gave a different real location
  (the Methodist Street parking lot). The organizer is the source of
  truth for its own event's location, not a secondhand calendar listing.
- **Some ticketing/event platforms block verification the same way
  funeral-home sites do (see death-notices skill) — don't silently treat
  an unverifiable link as confirmed.** `thekate.org` 403'd both a second
  show listing (Clinton run — left out entirely) and a Regal Cinemas
  ticketing page (Waterford run — used anyway since multiple independent
  third-party listings agreed on it, but flagged in the article's editor's
  note as not directly verified). Prefer leaving an item out over using an
  unverified link; if it's used anyway because independent sources
  converge, say so in the note rather than presenting it as fetch-verified.
- **A single-source fact can ship if it's flagged, rather than dropped
  outright, when the source itself is credible** (contrast with the
  Prismic/Danielle Nicole cases above, where the claim was actively
  contradicted by a primary source). The Lyme-Old Lyme-East Lyme run kept
  a real touring musician's appearance at a specific market on a specific
  date sourced only from one Patch listing, with no second source
  confirming that date — the difference from a "drop it" case is whether
  anything contradicts the claim, not just whether a second source exists.
- **A genuinely light town is a valid outcome, not a failure.** Clinton
  had no verifiable film or performance at all that week — two
  exhibitions were the whole article, stated plainly rather than padded.
  Orange had no verifiable Friday event at all and said so. Waterford had
  nothing beyond its own multiplex and leaned on neighboring New London,
  transparently flagged in the copy — a valid pattern, not a rule
  violation, per the neighboring-town address-format guidance above.
- **Running five towns as parallel forked subagents worked cleanly** —
  each one independently re-derived and applied the verification
  discipline above without cross-contamination between towns, and each
  sent its own email and log line without collision.

### Five-town batch #2, Sept. 2026 (Ledyard, Bethany-Woodbridge,
Durham-Middlefield, Montville, Newington) — run in parallel via forked
subagents

All five sent successfully. Two more confirmed instances of the "same
name, wrong place" pattern, plus a genuinely new hallucination variant
worth tracking separately:

- **A WebSearch synthesis can fabricate an event by blending two
  unrelated real results together, not just get one result's date/URL
  wrong.** The Bethany-Woodbridge run got a confident, specific-sounding
  "New Moon Hootenanny 2026" at "Three Saints Park Parking in Bethany" —
  but the underlying sources it was built from were actually an unrelated
  "Hunter's Moon Hootenanny" in Shelton, CT and an unrelated Instagram
  post. Neither source mentioned the event as reported; the search
  synthesis had merged them into something that looked like a single,
  real, sourced event. **Treat a WebSearch-synthesized answer as
  unverified even when it reads as confident and specific — the
  confidence of the phrasing is not evidence.** This is distinct from
  the already-documented "stale date" and "wrong venue name" failures:
  here the *event itself* didn't exist as described anywhere.
- **A templated, generic-sounding description repeated near-verbatim
  across multiple search results is itself a red flag**, independent of
  whether any single fact in it is checkable. The Ledyard run's
  "Hometown Fall Festival" had this shape — same boilerplate wording
  everywhere, no primary source — and turned out to be a garbled stand-in
  for a real, differently-named event (Holdridge Garden Center's
  "Holdridge Fall Fest") findable once traced to its actual organizer.
  When several results share suspiciously identical phrasing for an
  "event," search for the organizer/venue directly rather than trusting
  the repeated description.
- **Same-name-wrong-place, two more instances**: a "Montville Historical
  Society" WebSearch hit was for a same-named society in Montville, NJ;
  an "Added Color, Rudy G" listing at a real Bethany/Woodbridge-area arts
  venue (10selden.org) had no corroboration anywhere on that venue's own
  site — same unverifiable-listing shape as the Orange batch's 10selden
  case. Both dropped.
- **A recurring event's generic URL can carry a stale date baked into
  the slug itself.** Durham-Middlefield: a pub's own site linked a
  "Shawn Taylor" event page whose slug encoded a past date
  (`.../shawn-taylor-3-21-25/`) for what is actually a recurring series;
  the correct upcoming occurrence lived at a *different*, correctly-dated
  URL (`.../shawn-taylor-9-25-26/`) on the actual host venue's site. For
  any recurring-series event, don't assume the first URL found is the
  right occurrence — check that the date in the URL/page matches the
  date being reported before using it.
- **When two sources give conflicting street numbers for the same real
  venue, prefer the venue's own homepage/footer over a secondary
  listing site** (Durham-Middlefield: 70 vs. 72 Lyman Rd, resolved to 72
  via the venue's own footer).
- **A town's own Patch calendar can come back nearly empty or entirely
  off-topic for the requested window** (Newington: its two listed items
  were an out-of-town talk and a neighboring town's wine festival). When
  that happens, bypass Patch and go straight to likely organizer sites
  (town Chamber of Commerce, arts league, library, historical society)
  rather than stretching irrelevant Patch listings to fill the article.
- **Not every verification catches a problem — sometimes it just
  confirms the claim was right.** Newington's Chalk Walk URL came from a
  WebSearch AI-summary before being fetched directly; the direct fetch
  confirmed it was accurate. Keep verifying every claimed fact/URL
  regardless of source, but don't expect verification to always turn up
  an error — confirming a true claim is exactly what the step is for.
- **A postal address can name a different place than the town the venue
  actually sits in.** Ledyard: Foxwoods Resort Casino's postal city is
  "Mashantucket" (the reservation), but the property is geographically
  within Ledyard's town limits. Treated as in-town for the address-format
  rule (street address only) and flagged as a judgment call in the
  article's editor's note — unlike a clean village-within-town case, this
  one is worth a second look if it recurs elsewhere.

### Eight-town batch, Sept. 2026 (Danbury, Newtown, Brookfield, Bethel,
Southbury, Monroe, Wilton, Weston-Redding-Easton) — run in parallel via
forked subagents

This batch is where the shared-WebSearch-budget constraint (now
documented in Process above) first showed up clearly: Monroe, Wilton,
Bethel and Newtown's forks all explicitly reported exhausting the
200-call session budget mid-task, and Danbury, Monroe, Wilton and Bethel
all came back with noticeably thin articles (1-2 events for towns this
size) as a likely result. Newtown still managed 6 solid events despite
also hitting the ceiling, so budget exhaustion degrades results rather
than reliably capping every town at the same low count — don't assume a
thin result *isn't* budget-related just because another equally-capped
town did fine.

**The most dangerous same-name-wrong-place instance yet**: Newtown
research surfaced "The Newtown Theatre" with three shows that fit the
weekend suspiciously perfectly — a direct fetch of the venue's own site
showed it's in Newtown, **Pennsylvania**. Unlike earlier catches
(Waterford Gallery of Art/Ireland, gotomonroe.com/Michigan,
townofbrookfield.com/Wisconsin, a "State Theatre" in Easton/Pennsylvania,
westonpl.org/Ohio — all confirmed again this batch, this pattern is now
extremely common and should be treated as the default risk on every
single venue name, not an edge case), this one had **no qualifying word
distinguishing it from the target town at all** — just the bare town
name plus "Theatre," which is exactly the kind of venue name a real local
theater in the target town would also plausibly have. The only reason
it was caught was fetching the venue's own site directly per the
skill's standing rule, not any name-pattern heuristic. Treat every venue
name as needing its own site fetched and its address confirmed as
actually in the target state/town, with zero exceptions for how
plausible or exact the name match seems.

**A new site-block instance**: `wiltonlibrary.org` returned HTTP 403 to
both WebFetch and curl with a browser user agent — same Cloudflare-style
signature already documented for several funeral-home sites in the
death-notices skill, now confirmed for a library site too. Worked around
via two independent third-party directory listings that agreed on the
same details (a local news site, a chamber of commerce listing) rather
than trusting either alone.

**A WebFetch summary can invent a year that isn't on the page at all**
(distinct from the already-documented stale-date and wrong-day-of-week
failures): a Weston Public Library book fair page was summarized as
"2024," but the actual page text had no year printed anywhere — caught
because the stated day-of-week/date pairing (Sat Sept 26/Sun Sept 27)
only maps to 2026, cross-checked against a second independent source.
**When a summary states a year, verify that year is actually printed on
the source, not inferred by the summarizing step.**

**A secondary source's generic recurrence claim overrode by the venue's
own specific page**: one source described a recurring show as happening
"the first Thursday of the month" (which would've been the wrong date
for this window); the venue's own events page confirmed the actual
in-window date directly. Prefer a venue's own specific, dated listing
over any secondary source's generic recurrence description every time.

Also confirmed again this batch, same as prior batches: a stale-looking
URL slug doesn't always mean stale content (Danbury's Irish Festival page
had a "2025" slug but current 2026 body text — check the body, not just
the slug); a repeatedly-listed event across multiple secondary sources
can still be unconfirmable and droppable if the primary organizer's own
site and the specific permalink both fail to corroborate it (Bethel's
"Glenn Roth" case); and a town's own Patch calendar can return
essentially nothing for the actual target town (Bethel, Monroe, Wilton
all had this — Patch's calendar skewed almost entirely to neighboring
towns), requiring a fallback straight to organizer/local-aggregator
sites.

#### Follow-up: re-running the four thin towns solo (same session)

After this batch, Danbury, Monroe, Wilton and Bethel were each re-run
alone (not in parallel) to test whether their thin results were caused by
the shared WebSearch budget running out. Key finding: **the WebSearch
budget is per-session, not per-fork — a new fork in the same session
inherits whatever budget the session has already used, it does not get
its own fresh allocation.** By the time these four re-runs started, the
session was already at 0/200, so all four had to run WebFetch-only (direct
site fetches and educated URL/domain guesses, no search engine at all).
**A real "fresh budget" re-test requires a genuinely new session, not
another fork of an already-budget-exhausted one.**

Even WebFetch-only, results varied — thinness isn't always the same
underlying problem:
- **Danbury (1→3 events) and Wilton (2→3 events)**: real gains. Both
  found additional real, verifiable events via direct fetches of venues
  the first pass hadn't reached (a university gallery, a music centre;
  a walking tour whose name the first pass had gotten garbled via a
  secondary source). Confirms these were at least partly budget-starved,
  not genuinely this quiet.
- **Monroe (1→1)**: no new events found, but WebFetch-only research did
  find a real technical workaround worth reusing — a blocked/JS-rendered
  library calendar (the "whofi" platform) exposed a working structured
  data feed underneath at a predictable URL pattern:
  `whofi.com/calendar/rss/<start-date>/<end-date>`. This is the same
  "blocked HTML, working feed underneath" shape as the Tukios/FrontRunner
  funeral-home API workarounds in the death-notices skill — worth trying
  on any calendar platform that blocks its rendered page, before assuming
  it's a dead end. Monroe's thin result held up as genuine.
- **Wilton's library/town sites, by contrast, were confirmed hard dead
  ends**: wiltonlibrary.org and wiltonct.gov both 403'd with no
  discoverable feed/API workaround despite trying several guessed
  patterns. **Not every blocked calendar platform has a structured-data
  escape hatch — try a quick guess, but don't sink excessive effort into
  one blocked site if it doesn't pan out fast.**
- **Bethel was inconclusive, and the fork correctly declined to send a
  "(revised)" article rather than misrepresent it.** Without any search
  capability and without the first pass's specific source URLs on hand,
  it couldn't even rediscover the right domains to re-verify the
  *original* two events, let alone find new ones. **When a re-run can't
  actually improve on or even re-confirm the original due to a tooling
  constraint (not a content constraint), don't send a "revised" article
  that just re-asserts the same content — say so and leave the original
  as the final version instead.** This is the same standing principle as
  "don't pad with unconfirmed items," applied to the re-run case
  specifically.

**Practical guidance going forward**: if thoroughness across a large
batch matters, prefer running towns in smaller waves within one session
(so later waves still have search budget) over one giant parallel batch,
or plan for a follow-up session (fresh budget) to re-check any town that
comes back surprisingly thin for its size — don't rely on same-session
re-forking to fix it.

### Danbury — solo re-run, fresh session, Sept. 2026

A third independent run (after the original batch pass and the
same-session WebFetch-only re-run) confirmed the town is genuinely thin
on Thursday/Friday programming, not just budget-starved — this run had a
full fresh WebSearch budget and still found nothing verifiable before
Saturday. Ended up with the same three real events as the prior
re-run's ballpark (a WCSU gallery exhibit, the Irish Festival, one
concert), landed on this time via different specific finds. Two
technique notes worth keeping:

- **A dated Patch calendar URL (`?date=YYYY-MM-DD`) surfaced far more
  real events than the plain `/calendar` page.** The undated page for
  this town returned almost nothing for the target weekend; adding the
  explicit date query param returned a full, specific list (including
  the Irish Festival and a Palace Danbury show) that the undated fetch
  had missed entirely. Try the dated URL form before concluding a town's
  Patch calendar is empty for a given weekend.
- **A venue's own top-level domain can be a dead squatted site even when
  the venue is real and well-documented elsewhere.**
  `ivesconcertpark.com` 301-redirected to an unrelated personal site
  (`taraboulsi.com`) with no connection to the venue at all — a stronger
  and stranger failure than a 403 block or a same-name-wrong-state venue.
  A WebSearch synthesis had attached a specific "Paint Night at the Park"
  event to this venue/domain; since the domain itself doesn't resolve to
  anything venue-related, the claim was dropped outright rather than
  flagged, the same as the Bethany-Woodbridge "New Moon Hootenanny"
  fabricated-blend case.
- An exact show time can be unconfirmable even on the venue's own
  primary ticket-purchase page (Palace Danbury's MOSAICA event page gave
  date, price and lineup but no time) — printed as "Saturday evening"
  rather than borrowing a specific time from a secondary search
  synthesis that the primary source didn't corroborate.

### Newtown — solo re-run, fresh session, Sept. 2026

**"The Newtown Theatre" false positive recurred, unprompted, on the very
first search** — same plausible-fitting show pattern as the eight-town
batch already documented above (a bare town-name-plus-"Theatre" venue
with a show that fit the weekend suspiciously well). Direct fetch of
`thenewtowntheatre.com` again confirmed it's in Newtown, Pennsylvania.
This is now three-for-three on this specific trap recurring across
independent runs — treat it as close to guaranteed to resurface any time
this skill runs for Newtown, CT again, not a one-off fluke.

**A WebFetch summary computed wrong days-of-week for every single event
on a page that only prints bare dates.** Edmond Town Hall's site lists
events as "Sep 25," "Sep 26," "Sep 27" with no day-of-week text at all;
the first summarizing pass invented "Thursday," "Friday," "Saturday" for
those three dates respectively — each one wrong by exactly one day
(the correct days were Friday, Saturday, Sunday). A second, more literal
fetch asking for verbatim text (no day names on the page at all) caught
it. When a summary states a day-of-week the source page doesn't actually
print, don't trust it — compute it yourself from the bare date instead.

**A secondary news source's descriptive phrase for an event is not that
event's actual name or venue.** The Newtown Bee described a Sept. 27
Newtown Historical Society program as a "19th Century Educational
Experience" happening at "the meeting room of C.H. Booth Library" (that
detail actually belonged to a different, earlier Sept. 20 event in the
same article). The Historical Society's own site named the Sept. 27
event "Little Red School House Open House" and placed it at Middle Gate
Elementary School, 7 Cold Spring Rd — a different address entirely.
Confirmed via the organizer's own site rather than published under the
secondary source's invented name/location.

Ended with four solid, independently verified events (a tribute concert,
a free historical-anniversary double feature, a touring kids' musical,
and the historical society open house) — three of the four sharing one
venue (Edmond Town Hall) turned out fine for variety since each was a
different format/category; don't treat single-venue concentration alone
as a reason to pad with weaker items from elsewhere.

### Bethel — solo re-run, fresh session, Sept. 2026

This finally resolves the "inconclusive" Bethel case left open above: with
a full fresh WebSearch budget and no tooling constraint this time, the
town still came back thin — two verified events (a Thursday mural
unveiling, a Sunday jazz trio), nothing Friday or Saturday despite
checking the library, historical society, downtown venues and Patch
calendar directly. Three real, independently-checked runs now agree this
town is genuinely light on the Thu-Sun window specifically, not a
budget or tooling artifact.

- **The "Glenn Roth Live at La Zingara" listing already flagged as
  unconfirmable in the original batch resurfaced again, verbatim, across
  the same set of aggregator sites** (allevents.in, stayhappening, a
  generic "Bethel Jazz" search synthesis) — and this time a primary
  source was reachable to test it against: the organizer's own site,
  betheljazz.com/shows, lists no such date and confirms its regular
  series runs Wednesdays, not Sundays. This is a step beyond "can't
  corroborate" — the organizer's own page actively contradicts the
  aggregator claim, so it was dropped outright rather than flagged.
  Worth treating this specific listing as debunked if it appears again
  in a future Bethel run, not just unconfirmed.
- **"Bethel Woods Center for the Arts" is a new, sharp instance of the
  same-name-wrong-place trap** — a real, well-known, heavily-indexed
  venue (the Woodstock site) that happens to share the "Bethel" name,
  but is in Bethel, New York, not Bethel, Connecticut. Search results
  described specific fall 2026 programming there confidently enough that
  it would have been an easy inclusion without checking the state.
- **A single well-sourced item can still need two hops to confirm.** A
  local nonprofit's mural-unveiling event was first found via a
  hyperlocal aggregator (Bethel Grapevine) with full date/time/address;
  neither a broad WebSearch nor the nonprofit's Patronicity fundraising
  page corroborated the date, but the nonprofit's own `/events` page
  did, verbatim down to the time. Don't stop at "the fundraising/about
  page doesn't mention it" when the same organization plausibly has a
  separate, more current events page.
- **`ctfilmfest.com/bethel/` is a dead domain (connection refused, not a
  404)** — a Connecticut Film Festival Bethel event that multiple search
  results referenced could not be verified at all and was dropped;
  connection-level failures are as disqualifying as a 404 or a
  contradicted claim.

### Wilton — solo re-run, fresh session, Sept. 2026

A third real data point on this town, and it lines up with Bethel's:
genuinely thin (two events, both from the same organization, nothing
Friday or Saturday) despite a full fresh WebSearch budget and no tooling
constraints. `wiltonlibrary.org` was not re-tested this run (already
confirmed a hard 403 dead end twice) — went straight to the Historical
Society, Weir Farm, the Playshop, the Chamber of Commerce and Good
Morning Wilton instead.

- **The garbled-secondary-source walking tour name from the earlier
  batch recurred in a new form, confirming it's the source's problem, not
  a one-time error.** Patch's dated calendar listed a "Lambert House
  Walking Tour" for Saturday, Sept. 26 at 10 a.m., 150 Danbury Rd. The
  Historical Society's own site gives a different name ("Lambert
  Corners Walking Tour"), a different date (Sunday, Sept. 27, 10:30
  a.m.), and clarifies 150 Danbury Road is actually the Christmas Barn
  Shop where the tour *ends*, not where it starts — the walk itself
  begins at Lambert Corner (Rt. 7/33) and is led by town historian Bob
  Russell. Every one of Patch's four data points (name, day, time,
  address-meaning) was slightly off; only the organizer's own page got
  all four right.
- **Another WebSearch synthesis fabricated an event by attaching a real
  program's real recurring shape to a name that doesn't check out.** A
  search for Wilton weekend events produced a confident, specific claim
  — Weir Farm's monthly Open Studio series (real, confirmed via NPS) was
  happening Sept. 26 with an artist-in-residence named "Jan-Ru Wan." No
  independent trace of that name exists anywhere. Worse, the article the
  synthesis cited as its source (a Good Morning Wilton roundup) was
  fetched directly and contains no mention of Weir Farm, Open Studios, or
  that name at all — and Friends of Weir Farm's own events page
  separately confirmed no Open Studio is scheduled that weekend. Same
  failure shape as the Bethany-Woodbridge "New Moon Hootenanny" case:
  synthesis presented as if quoting a real source that says nothing of
  the kind.
- **A nav-menu "current exhibitions" listing can include a long-since
  digitized/permanent online exhibit alongside genuinely current
  in-gallery shows** — the Historical Society's site lists "Citizens at
  Last: Hannah Ambler, Grace Schenck and the Vote" as a current
  exhibition, but it's actually a 2020 online-only exhibit (created for
  the 19th Amendment centennial) with no physical installation or
  specific run dates. Skipped rather than presented as an in-person
  gallery show worth a visit this weekend.

### Brookfield — solo re-run, fresh session, Sept. 2026

A useful contrast after three thin towns in a row: this one came back
genuinely full (four solid events — a theater run, a craft-instructor
exhibition, a library talk, a themed film screening) after the same
level of verification effort, confirming the recent thinness has been
about the towns, not a drop in research thoroughness. All three main
venues (library, theater, craft center) cluster within a few hundred
feet of each other on Whisconier Road — worth remembering as a
first-look area for this town in future runs.

- **A WebSearch synthesis attributed a real, verifiable event to the
  wrong town outright.** A "History Through a Lens" photography workshop
  surfaced as a Brookfield-area event for Saturday, Sept. 27, matching
  the target weekend closely enough to be tempting — but the actual
  program (confirmed via a direct listing citing the host organization)
  is run by and held at the Weston History & Culture Center in Weston,
  CT, a neighboring but different town. Excluded on town grounds alone,
  independent of whether the event itself was real.
- **A venue's own homepage can show a stale "current" exhibition** while
  a dedicated events/calendar subpage on the same site has the accurate,
  current listing — Brookfield Craft Center's homepage still displayed a
  show that had already closed in July; its `/coming-events/` page
  correctly showed the actual current exhibition (Sept. 5–Oct. 17). When
  a homepage and an events subpage on the same domain disagree, prefer
  the more specific/dedicated page, the same principle as trusting a
  library's dedicated events list over its general homepage blurb.
- A Patch-sourced library listing ("Here Comes the Bride: Wedding
  Dresses Through the Years") turned out to need a name-format
  normalization to find on the library's own site (prefixed there as
  "OTOR: Here Comes the Bride...", tying it to the library's One Town,
  One Read program) but was otherwise accurate on date, time and
  description — a case of Patch being right on substance even when its
  exact title string doesn't match the primary source verbatim.

### Monroe — solo re-run, fresh session, Sept. 2026

**"Monroe" turned out to be an unusually bad town name for this skill —
six separate same-name-wrong-place hits in one research pass**, more
than any other town so far by a wide margin: a "Monroe Arts Center" in
Monroe, Wisconsin (giveaway: a 608 area code); a "Monroe Arts
Association" in Monroe, Oregon; a "Monroe Township Cultural Arts
Commission" and an "Etsch Farms" corn maze both in Monroe Township, New
Jersey; a "Monroe Arts Council" apparently in Washington state; and the
already-known gotomonroe.com (Michigan) from the original batch. Every
one of these would have read as a perfectly plausible local hit without
checking the state/area code. For this specific town, treat "checking
the state" as mandatory on literally every search result, not just the
ones that seem too on-the-nose.

After excluding all of those, genuinely nothing verifiable turned up for
film, exhibition or performance categories in the Thu-Sun window — a
first for this skill (every other thin town so far still had one or two
real items). Checked and came up empty: the library's whofi calendar
(loaded fine this run, no 403; contents were routine kids/teen
programming, not arts-and-entertainment-category events), the
Historical Society (next event Oct. 4, outside the window), Wolfe
Park's summer concert series (ended in August), and Masuk High School
Drama (no fall production found). `themonroesun.com`, a local news
source that had been useful for other thin towns' hyperlocal coverage,
403'd here.

Flagged this to the user before sending rather than assuming how to
handle a true zero — the user chose to broaden scope to a recreational
fall attraction (Benedict's Home and Garden's corn maze/hayrides,
verified directly on the farm's own site) rather than send a note-only
piece or skip the town. **This is now standing policy, not a one-off
call**: the Process section above formally adds recreation/family
attractions (farm fall-fun, fairs, festivals, seasonal outdoor
attractions) as a fourth category, to be used as a fallback when a town
is thin or empty in the traditional three, not as a default alongside
them. A future true-zero town no longer needs a check-in for this
specific question — reach for recreation automatically per the Process
rule — though checking in is still right for anything else genuinely
ambiguous.

### Weston-Redding-Easton — solo re-run, fresh session, Sept. 2026

A genuinely full week this time (five events, all three towns
represented, spanning Thursday through Sunday) with two reusable
technique notes for this specific combined town:

- **The Patch calendar URL for this trio is not the naive
  `weston`/`redding`/`easton` slug — it's `weston-ct`.** All three towns
  share one combined Patch presence at
  `patch.com/connecticut/weston-ct/calendar`, and `easton-ct` also works
  independently; plain `/connecticut/weston/calendar`,
  `/connecticut/redding/calendar` and `/connecticut/easton/calendar`
  all 404. Try `weston-ct` first for this combination.
- **The correct Weston Public Library domain is
  `westonpubliclibrary.org`, not `westonpl.org`** — the latter is the
  already-documented Ohio trap from the original eight-town batch.
  Getting the domain from a specific search result (rather than
  guessing a shortened form) avoided the trap entirely this run; worth
  doing deliberately for this town rather than guessing the library's
  URL pattern.
