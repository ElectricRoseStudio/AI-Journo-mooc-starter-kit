# All-weekend skill

Writes a single combined Patch-style hyperlocal weekend round-up for a
town (or a town plus its immediately adjacent towns, if explicitly
asked): one article covering both the arts/entertainment scene (per the
`arts-roundup` skill) and the restaurant/bar scene (per the
`weekend-eats` skill) for the coming Thursday through Sunday, with real,
verified venue links and Google Maps links throughout.

This skill is a merge, not a replacement — it inherits both source
skills' research categories, verification discipline and style rules
wholesale. Read `skills/arts-roundup.md` and `skills/weekend-eats.md`
in full before running this skill; their Customization sections hold
town-specific and platform-specific traps (same-name-wrong-place
venues, stale chamber-of-commerce pages, fabricated WebSearch bookings,
day-of-week mismatches) that apply here exactly as they do in the
source skills. This file does not repeat that material — it only
documents what's specific to combining the two.

## When to use

Invoke this skill when asked for one article covering everything
happening in a town this weekend — arts and dining together — e.g. "run
all-weekend for New Milford" or "do a full weekend guide for Ridgefield."
If the user asks for just the arts angle or just the restaurant angle,
use the corresponding single skill instead; don't default to the
combined version.

Scope rule matches `weekend-eats`: single town by default, widened to
adjacent towns only when the user explicitly says so.

## Process

1. **Establish the target window**, same as both source skills.
2. **Research both category sets** for the town(s):
   - Arts categories (from `arts-roundup`): films, exhibitions,
     performances, and recreation/family attractions (recreation as a
     fallback for a thin week, not a default — see that skill's Process
     section).
   - Dining categories (from `weekend-eats`): special brunches,
     dinner-and-a-show/dinner theater, live music at bars and
     restaurants, food festivals, Restaurant Week-style promotions.
     This inherits `weekend-eats`' exclusion of drag brunches and any
     other event centered on drag performance — a category exclusion,
     not a verification judgment, so it applies even to a real,
     well-confirmed, in-town booking.
   - Use each skill's source-reliability guidance for its own category
     set (venue's own site/ticketing subdomain first, dated Patch
     calendar, WebSearch as discovery only). A single dated Patch
     calendar fetch typically surfaces candidates for both category
     sets at once — sort what it returns into the right bucket rather
     than fetching the calendar twice.
3. **Verify every fact** per both skills' standing rules — direct-fetch
   every URL, cross-check day-of-week against date, don't trust a
   generic reputation claim as a dated booking, treat WebSearch
   synthesis as capable of fabricating a specific booking at a real
   venue.
4. **Resolve category overlap once, not twice.** A dinner-theater event
   (BYO cabaret seating, a ticketed dinner-and-a-show package) fits both
   skills' definitions. List it exactly once — under Restaurants & Bars,
   since the dining format is what distinguishes it from an ordinary
   performance — and don't repeat it under Arts & Entertainment.
5. **Organize the article as one piece with two labeled sections**
   (see Article format below) rather than either interleaving every
   entry chronologically or publishing what reads like two separate
   articles stapled together. Apply each source skill's "favor variety,
   don't pad a thin week" guidance independently within its own
   section — a strong arts week and a thin dining week (or vice versa)
   is a normal, valid outcome and shouldn't be evened out by borrowing
   items across sections just to balance the count.
6. Write the article per the format and style rules below.

## Article format

SEO headline, no longer than 109 characters, naming the town and
signaling that this covers the whole weekend (not just one slice of
it) — e.g. "New Milford's Full Weekend Guide: ...". Initial caps on
every word, same as both source skills.

Meta description, up to 136 characters.

One engaging intro paragraph setting up the weekend as a whole before
either section starts.

**This Weekend's Arts & Entertainment** — a bulleted list per the
`arts-roundup` skill's list-item format (title; day/time; one to two
sentences of sourced description; venue name hyperlinked to its
official site; address hyperlinked to Google Maps).

**This Weekend's Restaurants & Bars** — a bulleted list per the
`weekend-eats` skill's list-item format (same structure, plus price/
BYO/age-restriction details where relevant).

Closing paragraph tying the whole weekend together, referencing
specifics from both sections rather than a generic "something for
everyone" line.

## Style rules

Identical to both source skills: AP style per this repo's `CLAUDE.md`
(no Oxford comma, spell out numbers under 10, dollar amounts as `$45`),
times as `8 p.m.`, dates as `Sept. 26`, the shared venue-address format
rule (visible text omits state/ZIP always, omits the town name only for
venues inside the target town, Google Maps URL always uses the full
address), and never fabricate or guess a URL — every link must be
fetched and confirmed to resolve.

## Delivery

Same disposable-script send mechanism as the other two skills:
`smtplib`, credentials from `~/.config/newtown-mail.env` (`set -a`
before sourcing), `From: "Patch_Edit_AI <rich@electricrose.net>"`, sent
as HTML, a line appended to `beat-archive/send-log.txt`. Subject line
convention: `[Town] all-weekend round-up — draft article`. "Send it to
me" means the user's own address, not a default Patch-colleague
recipient.

---

## Customization

Add findings specific to *combining* the two skills below this line —
findings specific to one category alone belong in that category's own
skill file (`arts-roundup.md` or `weekend-eats.md`), not here.

<!-- Your additions here -->

### New Milford — first run, Sept. 2026

The combined format worked cleanly on its first outing (two arts
entries, five dining entries, no overlap disputes) and validated the
core reason to run this skill instead of the two separate ones:

- **Merryall Center for the Arts's Hildaland concert was excluded from
  a `weekend-eats`-only pass on this same town** (it's a theater/arts
  space, not a restaurant or bar) **but correctly belongs in this
  skill's Arts & Entertainment section.** This is exactly the case this
  skill exists for — a real, well-verified event that one single-topic
  skill would legitimately drop for being out of its category, but
  which a full weekend guide should include. When running this skill,
  don't discard something just because it wouldn't fit `weekend-eats`
  alone; check whether it fits the arts side before dropping it.
- **The New Milford Apple Festival is the reverse case**: it has real
  arts-adjacent flavor (a baking contest, "entertainment") but is
  fundamentally a food/vendor festival, so it stayed in Restaurants &
  Bars per this skill's overlap rule (list once, in whichever section
  the event is more fundamentally about) rather than being duplicated
  or moved to Arts & Entertainment.
- **Both already-documented traps for this town recurred and were
  caught the same way as before**: the fabricated "Prismic" exhibit at
  Village Center for the Arts (still absent from the org's own events
  calendar, exactly as found in the original five-town arts-roundup
  batch) and a new one, "Gallery on the Green," which several listings
  attributed to New Milford but which is actually in Canton, CT.
  Running this skill for a town already covered by one of the source
  skills doesn't mean the old traps have gone away — re-verify from
  scratch rather than assuming a previously-dropped claim is now safe
  to include, or a previously-unseen claim is automatically trustworthy.

### Danbury — first run, Sept. 2026

Reused three already-verified arts-side facts from the same-session
`arts-roundup` runs on this town (the WCSU exhibit, the Irish Festival,
Mosaica) rather than re-fetching them from scratch — reasonable within
the same session since they were direct-fetch-confirmed hours earlier
and nothing about them is time-sensitive in a way that would have
changed; re-verify instead of reusing if resuming in a new session or
after enough time has passed that hours/schedules could plausibly have
changed.

- **Danbury Restaurant Week already happened this year (July 10-18) and
  won't recur in this window** — worth remembering so a future Danbury
  run for either this skill or `weekend-eats` doesn't waste a search
  cycle looking for a September occurrence that doesn't exist.
- **Molly Darcy's Irish Pub is another instance of the TerraSole
  "standing feature, no confirmable specific date" case** documented in
  `weekend-eats.md` — real, well-documented weekend live entertainment,
  but no specific act/date found for this exact weekend on the venue's
  own site. Described as a standing feature rather than dropped, same
  resolution as TerraSole.
- **CityCenter Danbury's per-venue event pages (e.g.
  `citycenterdanbury.com/venue/two-steps-downtown-grill/`) are a good,
  fast way to confirm a *negative*** — the page explicitly returned "no
  results found" for the window, which is a cleaner signal to drop a
  candidate than an inconclusive homepage fetch would have been.

### Newtown — first run, Sept. 2026

**Aquila's Nest Vineyards (56 Pole Bridge Road) is worth checking first
for this town going forward** — its own `/visit-us` page listed a
specific, dated act or ticketed show for all four days of the window at
once (Thu-Sun), each independently confirmed with exact times and, for
the ticketed ones, prices. This is the richest single-venue source
found across either skill so far for any town; four solid, real,
distinct entries came from one fetch. Don't feel obligated to use every
one of them just because they're all confirmed — this run picked three
of the four (Thursday, Saturday, Sunday) and let Edmond Town Hall's
already-confirmed Friday show cover that day instead, per the "favor
variety, don't just stack one venue" principle.

- **A venue name reported by search can be subtly garbled**: "New
  Psylum" turned out to be "NewSylum Brewing Co." (a wordplay on
  "asylum," not "psylum"). Worth trying an obvious respelling before
  concluding a venue doesn't exist, the same lesson as the Ledyard
  "Hometown Fall Festival" → "Holdridge Fall Fest" case already
  documented in `arts-roundup.md`.
- **SBC Restaurant and Brewery, which several searches surfaced for
  "Newtown," is actually in Milford, CT** — a neighboring-region
  mixup, not a same-state trap, but excluded on the same "check the
  town" principle as Fife 'n Drum in the New Milford run.
- **A restaurant's ticketed-event pricing can carry small inconsistencies
  across listings worth flattening rather than reporting precisely** —
  Aquila's own site gave the comedy show as a "6-9 p.m." window while a
  ticketing platform listed a 7 p.m. start and a price with odd cents
  ($26.50 rounding from a base price plus fees). Used the venue's own
  time window and rounded the price ("around $26") rather than
  presenting a fee-inflated ticketing-platform figure as the actual
  price.

### Bethel — first run, Sept. 2026

The combined format doesn't rescue a town that's genuinely thin across
*both* categories — this run landed at three total entries (one arts,
two dining), the smallest all-weekend article so far, and that's a
correct reflection of the town rather than a research shortfall. Two
notes:

- **Nearly every non-restaurant "arts" lead already found for Bethel in
  the `arts-roundup` skill's prior passes turned out to actually belong
  in the dining category once this skill's two-section split was
  applied** (Portofino's jazz series, the debunked La Zingara listing) —
  Bethel's real programming this specific weekend skews almost entirely
  toward bars/restaurants, with the mural unveiling as the only
  standalone arts item. Don't force a second arts entry to balance the
  two sections if the town's actual events don't support it.
- **The already-debunked "Glenn Roth Live at La Zingara" listing
  surfaced a third time** (see `weekend-eats.md`'s Bethel notes for the
  first debunking). It's now resurfaced across two independent
  all-weekend-adjacent research passes on this town — treat it as a
  standing, recurring false positive for Bethel specifically, not just
  a one-off aggregator error, and expect to re-exclude it essentially
  every time this town comes up again until the underlying bad listing
  is corrected at the source.

### Wilton — first run, Sept. 2026

Reused the two already-verified Wilton Historical Society arts entries
from the earlier same-session `arts-roundup` run rather than
re-fetching. Two new findings on the dining side:

- **A same-named-business-chain trap, distinct from a same-named-venue
  trap**: "The Social Bar + Kitchen," which surfaced as hosting a drag
  brunch "in Wilton," is a real chain with a real event — just at its
  New London location, not a Wilton one. Similarly, "Spotted Horse
  Tavern" (also chain-like, multiple CT locations) surfaced as a Wilton
  live-music bar but only has Westport and Shelton locations. When a
  business name suggests a small local chain, check that the specific
  location cited actually has a Wilton address, not just that the brand
  operates somewhere in the region.
- **A dedicated venue events page stating "No upcoming events" is a
  clean, confirmed negative — use it to drop the lead outright**, not
  to fall back on describing a "standing weekly feature" the way
  TerraSole/Molly Darcy's/Notch8 were handled elsewhere. Little Pub
  Wilton's own `/events` page explicitly said nothing was scheduled,
  which is a stronger and more specific signal than those other cases
  (which had no dedicated events page to check at all, only a vague
  homepage claim). Reserve the "standing feature, no pinned date"
  treatment for cases where nothing contradicts the recurring claim,
  not for cases where a primary source actively says there's nothing
  this week.

### Brookfield — first run, Sept. 2026

Reused the four already-verified arts entries from the earlier
same-session `arts-roundup` run. The dining side needed real digging —
several strong-looking leads failed for different reasons before three
solid ones turned up:

- **"Imperial Oak Brewing," which multiple searches attributed to
  Brookfield with a specific Thursday bingo night, is in Brookfield,
  Illinois** — this town name is now confirmed prone to the same
  multi-state collision problem as Monroe, not just the single
  Wisconsin case already documented in `arts-roundup.md`. Treat every
  "Brookfield" venue name with the same state-checking discipline
  applied to Monroe.
- **A WebSearch synthesis relocated a real, already-verified
  Ridgefield venue (TerraSole) into Brookfield** — not a fabricated
  booking this time, but an entire real business misattributed to the
  wrong town outright. Caught only because TerraSole's actual address
  (3 Big Shop Lane, Ridgefield) was already known from an earlier
  session; a fresh session without that context could easily have
  missed this one.
- **The drag-brunch exclusion rule (added after the Wilton run) applied
  here on the first real test**: the same "Pop vs. R&B Pink Eggs & Glam
  Drag Brunch w/ Wesley" listing surfaced yet again, this time
  attributed to Brookfield. It's excluded twice over now — wrong town
  (it's the New London Social Bar + Kitchen) and, regardless of town,
  categorically excluded by rule. This specific listing has now falsely
  attached itself to two different towns (Wilton, Brookfield) across
  research passes — a search-synthesis pattern of grabbing one real
  regional event and scattering it across whatever town is being
  searched.
- **A seasonal restaurant's own site can go dark for the off-season
  before independent sources catch up.** Down The Hatch, a real
  seasonal lakefront restaurant, had a Yelp listing flagged "TEMP.
  CLOSED" and its own events page just said "Coming Soon" — dropped
  rather than assumed open, even though nothing explicitly said it was
  closed for this specific week.
- **A one-time or seasonal past event can resurface in search results
  looking current** — a "Jazz and Champagne Brunch" at the Candlewood
  Inn was actually a one-off March 2026 fundraiser tied to the Danbury
  Music Centre, not a recurring weekend feature. Checked the date
  before treating a promising-sounding brunch lead as ongoing.
- **A primary source blocked with a 403 doesn't automatically sink a
  lead if multiple independent secondary sources agree on the same
  specifics** — Twins BBQ Co.'s corn maze page 403'd, but a dated local
  news article, Patch and a family-events listing all independently
  confirmed the same dates, hours and price, so it was included with an
  editor's-note flag rather than dropped, per the established
  precedent for blocked sites in `arts-roundup.md`.

### Monroe — second all-weekend-adjacent run, Sept. 2026

The combined format still can't produce content a town doesn't have:
this landed at one total entry (Benedict's Home and Garden's fall fun,
carried over from the original `arts-roundup` run), with the dining
side coming back completely empty on top of the already-empty arts
side. Two new, useful findings:

- **A brewery can close and have its domain repurposed for an unrelated
  business, the same failure shape as `ivesconcertpark.com` redirecting
  to a personal site in the Danbury `arts-roundup` run.** Veracious
  Brewing Company is still listed as open, with weekend live music,
  karaoke and trivia, by multiple secondary sources (Tripadvisor,
  Untappd, general web search) — but its own domain,
  veraciousbrewing.com, now 301-redirects to an unrelated sushi
  restaurant's site, and its Yelp listing is separately flagged
  "CLOSED." Two independent signals of closure (the dead-domain
  redirect and the Yelp closure flag) is a strong basis to drop a venue
  entirely, not just the specific event claimed for it.
- **Monroe Restaurant Week already happened (Sept. 14-20) and, unlike
  most towns' restaurant weeks, ended *before* this window rather than
  spanning it** — the reverse timing from Wilton's, which ran
  underneath the whole window. Confirm a restaurant week's actual dates
  against the target window every time; don't assume "there's a
  restaurant week this month" means it's still running.
- **Monroe, Wisconsin surfaced yet again** (a real cheese festival,
  Green County Cheese Days) — now confirmed across arts and dining
  research alike as the most common wrong-state collision for this
  town, alongside the previously documented Oregon, New Jersey,
  Washington and Michigan cases.

### Southbury — first run, Sept. 2026

A clean example of the overlap rule working as intended: the Southbury
Celebration and Apple Festival were originally written up under
`arts-roundup` (before this skill existed) but both belong under
Restaurants & Bars here — both are fundamentally food-court/vendor
festivals with live entertainment as a secondary feature, not the
other way around. Reused the two genuinely-arts entries
(the library talk, the Remember Warsaw screening) as-is.

- **Southbury Restaurant Week already happened (April 10-18) and, like
  Monroe's, falls well outside this window** — a second confirmed case
  of a town's restaurant week not overlapping the Thu-Sun window at
  all. Don't assume any given September weekend automatically falls
  inside a "restaurant week" just because a town has one; check the
  actual dates every time.
- **A venue name style ("The Rathskeller," "The Tavern") can belong to
  multiple same-named venues across different CT towns** (a Southbury
  Rathskeller confirmed distinct from a Charlestown, RI one that
  surfaced in the same search) — generic tavern-style names need the
  same address-checking discipline as a distinctive one, maybe more,
  since there's no obviously "wrong" signal in the name itself the way
  there is with a real place name like a town or state.

### Weston-Redding-Easton — second run, Sept. 2026

Reused the five already-verified arts entries from the earlier
same-session `arts-roundup` run, then re-sorted two of them: the
44th Annual Artisan Fair moved from Arts & Entertainment to
Restaurants & Bars (it's a craft/food vendor fair with entertainment as
a secondary feature, the same overlap-resolution logic applied to
Southbury's Apple Festival). Found two new confirmed Milestone
Georgetown bookings for the dining side.

- **A WebSearch synthesis can turn a listed performer's name into a
  fictional second venue.** Milestone's own live-music page lists
  "Pimpinella" as a Saturday 9 p.m. booking (almost certainly a band or
  act name), but a separate search described "Pimpinella" as if it
  were an entirely different bar "just over the Redding line," with its
  own reputation for patio live music — language that was actually
  describing Milestone itself, misattributed to a new, nonexistent
  venue built from the act's name. Trusted Milestone's own dedicated
  events page over the synthesis and treated Pimpinella as a performer
  at Milestone, not a separate stop.
- **A specific street number for the same real venue can differ between
  a general web search and the venue's own site** — a search gave "2
  Georgetown Rd" for Milestone Georgetown, while the venue's own
  `/location/` page gives "2 Main St" (matching what was already used
  for this venue in the Ridgefield-area weekend-eats run). Prefer the
  venue's own site over a search-synthesized address even when the
  synthesis sounds specific and plausible.
- **The drag-brunch listing that's now followed this same regional
  search across three separate towns/regions** (Wilton, Brookfield, and
  now this three-town area) reinforces that it's not town-specific
  noise — it appears to attach to almost any Fairfield County-adjacent
  search for this kind of content. Expect to keep excluding it by rule
  regardless of what town or region turns it up next.

### Bethany-Woodbridge — first run, Sept. 2026

**Do not confuse this pairing's Patch presence with a hyphenated
slug** — unlike Weston-Redding-Easton's `weston-ct`, this combined pair
uses the portmanteau `bethwood` (`patch.com/connecticut/bethwood/
calendar`). Worth checking for a portmanteau slug on any future
combined-town pairing rather than assuming the hyphenated-town-name
pattern is universal.

The dining side came back completely empty — a second true zero
alongside Monroe's, but for a different reason: not thinness so much as
an unusually concentrated run of wrong-place traps, each independently
confirmed:

- **Two Bethany-named venues ("Mangos" and "Bethany Boathouse") both
  turned out to be in Bethany Beach, Delaware**, not this Bethany —
  worth remembering "Bethany" collides with a well-known Delaware beach
  town the same way "Brookfield" collides with Illinois and Wisconsin,
  and "Monroe" collides with half a dozen states.
- **"Bethany Vineyard & Winery" (bethanyvineyard.com) is a real,
  long-established winery — in Ridgefield, Washington**, not Bethany,
  CT. Unlike some of this project's wrong-place cases, this one wasn't
  a domain hijack or a synthesis error — it's simply a real out-of-state
  business with a name that happens to describe this Bethany perfectly.
- **Woodbridge Brewing Co.'s contact phone number carries a 732 area
  code (New Jersey), a strong signal the venue itself may not be this
  Woodbridge** even though its events page (confirmed empty for the
  window regardless) didn't say so directly — an area code is worth
  reading as a location signal even when a business doesn't name its
  city outright.
- **A restaurant's own domain can silently point to a completely
  unrelated business due to a certificate/hosting mismatch, not just an
  intentional redirect** — fetching Teddy B's Restaurant & Pizzeria's
  site (teddybsfood.com) actually served content for an unrelated
  Mediterranean restaurant in Warwick, NY, flagged by a TLS certificate
  name mismatch. Treated as unconfirmable and dropped, the same
  disposition as an explicit redirect to an unrelated site
  (`ivesconcertpark.com`, `veraciousbrewing.com`), even though the
  underlying technical cause was different.
- **When a real, well-documented event (a food-truck vendor's
  recurring visit to a real local farm) can only be confirmed for a
  stale past date** (Oct. 26, 2025), don't use it to imply a recurring
  weekly booking — Clover Nook Farm's own standing seasonal hours and
  U-pick pumpkin patch (independently confirmed) were used instead of
  the specific, outdated food-truck claim.
- Reused Woodbridge Like Me Day, a rich, well-documented town event,
  from the town's own site — a good example of a single town-run
  celebration carrying an entire arts section on its own when nothing
  else in either category is confirmable.

### Chester-Essex-Deep River — first run, Sept. 2026

**A third distinct Patch-slug pattern for a combined town**: this trio
uses `essex-chester-deepriver` (town order doesn't match the
alphabetical/requested order, and "Deep River" loses its space) — a
third variant alongside Weston-Redding-Easton's `weston-ct` and
Bethany-Woodbridge's portmanteau `bethwood`. There is no single rule
for guessing a combined-town Patch slug; search for it fresh each time
rather than pattern-matching from a previous combination.

- **This session hit its 200-call WebSearch budget mid-run** — the
  exact shared-budget constraint the original `arts-roundup` batches
  documented, now confirmed to apply to solo all-weekend sessions too
  once enough towns accumulate in one session. Recovered by switching
  to WebFetch-only for the remainder (direct site fetches, a Wikipedia
  fallback for a blocked business site) rather than stopping research
  — consistent with the original skill's guidance that WebFetch-only
  research is a real fallback, not a dead end, once the budget is gone.
- **"Essex" is a real English county with its own library system and
  event listings** — a "Retro Games" search result initially pointed at
  `library-events.essex.gov.uk`, a UK site, before the actual Essex,
  CT event was confirmed directly on the Essex Library Association's
  own site (`youressexlibrary.org`). This is a new *international*
  variant of the same-name-wrong-place trap, not just a wrong-US-state
  one — worth watching for on any town whose name also matches a
  well-known place abroad.
- **A theater can be genuinely dark between productions, and that's a
  legitimate, checkable finding, not a research gap.** Ivoryton
  Playhouse's prior show closed Sept. 6 and its next one doesn't open
  until Oct. 1 — confirmed via the specific show pages rather than
  assumed from the theater "usually" having something running.
- **A very old, well-documented institution's specific recurring
  tradition can be sourced from an encyclopedia entry when the
  business's own site is unreachable** — the Griswold Inn's site
  returned repeated 307 redirects and Yelp 403'd, but Wikipedia
  confirmed the address and the specific, dateable detail (a Sunday
  Hunt Breakfast tradition "existing for approximately two hundred
  years") independently. An encyclopedia entry is a legitimate primary-
  ish source for a long-standing institutional fact like this, distinct
  from trusting a WebSearch synthesis for a dated, specific booking.

### Lyme-East Lyme-Old Lyme — first run, Sept. 2026 (WebSearch-exhausted)

This run started with WebSearch already at 0/200 for the session (spent
during the Chester-Essex-Deep River run immediately before it), so it
was WebFetch-only from the start rather than degrading partway through.
Told the user up front rather than silently producing a thinner
article. Confirms the original `arts-roundup` batches' finding that
WebFetch-only research is a real fallback, not a dead end — this run
still produced four solid arts entries by fetching known/likely
institution URLs directly (Florence Griswold Museum, Lyme Art
Association) rather than discovering them via search, though the
dining side came back much thinner (one entry) than a fresh-budget run
would likely find.

- **A known "nickname" domain can be the correct redirect target, not a
  hijack** — `florencegriswoldmuseum.org` 301-redirects to `flogris.org`
  (the museum's actual, commonly-used short name). Worth distinguishing
  this from the `ivesconcertpark.com`/`veraciousbrewing.com` pattern of
  a domain redirecting to a genuinely unrelated business: check what
  the redirect target actually contains before assuming a redirect is
  automatically a bad sign.
- **This run independently re-confirmed the original arts-roundup
  batch's Niantic Main Street finding** (a farmers market's real
  location is the Methodist Street parking lot, not the "Smith's Acres
  LLC" address Patch's calendar gives it) — fetched directly from
  nianticmainstreet.org without needing to search for it, since the
  correct organizer site was already known from that earlier batch's
  documented finding. A specific fact recorded in an earlier
  Customization note can be reused as a starting point for direct
  verification even in a WebSearch-constrained session.
- **A second, unrelated TLS failure mode**: the Bee and Thistle Inn's
  site returned "certificate has expired" (not a name mismatch like
  Teddy B's, not a redirect like Griswold Inn) on every scheme tried.
  Treated the same as the other two — unconfirmable, dropped, disclosed
  in an editor's note — regardless of the specific technical cause.

**Follow-up, same day: a spawned general-purpose agent did NOT get a
fresh WebSearch budget.** Tried dispatching a fresh (non-fork)
general-purpose agent specifically to re-run this town's dining side
with its own search access, on the theory that the documented
budget-sharing problem only applied to forks ("a new fork in the same
session inherits whatever budget the session has already used"). The
agent reported its very first WebSearch call came back already at
0/200 — the budget is shared with *any* subagent spawned from an
exhausted session, fork or not, not just forks. **The only real fix
for an exhausted WebSearch budget is a genuinely new top-level session
(a new conversation), not any kind of in-session agent spawn.** Update
the earlier guidance accordingly: don't expect delegating to a fresh
subagent to route around this.

Despite the missing budget, the WebFetch-only agent still substantially
improved the dining section by directly fetching known/guessable
venues rather than discovering them via search:

- **Found a strong four-day anchor**: Bill Charlap Trio (with a Sunday
  solo matinee) at The Side Door Jazz Club, Old Lyme Inn, 85 Lyme St.,
  Old Lyme — Thu/Fri/Sat 8-10 p.m. plus two Sunday shows. Verified
  independently via both the venue's own site (thesidedoorjazz.com)
  and four separate Eventbrite ticket pages, one per date — a good
  example of a WebFetch-only pass still reaching a rich, fully
  cross-verified result when the venue's own site plus a ticketing
  platform both cooperate.
- **A third confirmation of the Niantic Farmers Market address
  discrepancy** (Methodist Street lot, not Patch's "Smith's Acres LLC"
  address) — now confirmed across three independent passes on this
  town. Treat this as permanently settled; stop re-verifying it as if
  it were still an open question.
- **The correct Patch slug for this trio is `thelymes`**
  (`patch.com/connecticut/thelymes/calendar`) — a fourth distinct
  slug-naming pattern for a combined town (alongside `weston-ct`,
  `bethwood`, and `essex-chester-deepriver`). Found via Patch's own
  connecticut nav rather than a guessed pattern — worth checking that
  nav directly for a combined town whenever a guessed slug 404s.
  Individual `old-lyme`/`east-lyme` slugs do not exist separately.
- **A specific fact one pass couldn't fully pin down (Wee Faerie
  Village's individual ticket price, $23/$22/$21) couldn't be
  re-confirmed by a second pass either** — the second agent found a
  *different*, general-admission price schedule ($18/$17/$16/$7.50) on
  the museum's homepage, but no page giving the specific Wee
  Faerie-only price directly. Kept the original, more specific source
  (a dedicated Wee Faerie Village page) over the more general one
  found later, consistent with the standing "prefer the more specific/
  dedicated page" rule — but this one is still not fully nailed down
  and is worth a targeted checkout-flow check if the exact number
  matters for publication.
- Several additional real, verified Niantic restaurants (Skippers
  Seafood, Castello of Niantic, Vincitori Apizza) turned up nothing
  confirmable for this specific weekend and were left out rather than
  padded in — a good example of thorough checking producing negative
  results rather than either false positives or unwarranted exclusion
  of the town's real dining scene.