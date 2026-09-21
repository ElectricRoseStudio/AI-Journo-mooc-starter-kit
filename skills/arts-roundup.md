# Arts-roundup skill

Writes a Patch-style hyperlocal "Weekend Arts Roundup" for a town: a
bulleted list of arts events, exhibitions, films and performances opening
or on view the coming Thursday through Sunday, with real, verified venue
links and Google Maps links.

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
   films, exhibitions (museums, galleries, libraries), and performances
   (theater, concerts, historical-society programs). Useful source types,
   roughly in order of reliability:
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
   film/exhibition/performance categories over a longer list of similar
   items. It's fine to have fewer than a "full" list if the town genuinely
   has a light week; don't pad with borderline or out-of-window items to
   hit a target count.
6. Write the article per the format and style rules below.

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
