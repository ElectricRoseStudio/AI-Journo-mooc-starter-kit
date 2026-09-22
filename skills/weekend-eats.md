# Weekend-eats skill

Writes a Patch-style hyperlocal "Weekend Eats" round-up for a town (or a
town plus its immediately adjacent towns): a bulleted list of special
restaurant- and bar-related events happening the coming Thursday through
Sunday — special brunches, dinner-and-a-show or dinner theater, live
music at bars and restaurants, food festivals, and Restaurant Week-style
promotions — with real, verified venue links and Google Maps links.

## When to use

Invoke this skill when asked to write a weekend restaurant/dining
round-up for a town, e.g. "write the weekend eats round-up for
Ridgefield" or "do a restaurant events article for New Milford this
weekend."

By default this covers a single town. Only widen it to immediately
adjacent towns when the user explicitly asks for that (e.g. "Ridgefield
or immediately adjacent towns") — don't widen scope on your own
initiative the way the recreation fallback in the arts-roundup skill
does, since restaurant coverage is naturally uneven town to town and
silently pulling in neighbors could misrepresent what's actually
happening in the requested town. If the user does widen scope, apply
the address-format rule below exactly as the arts-roundup skill's
multi-town rule does: omit the town name only for venues inside the
primary/target town, and name the town for every venue outside it.

## Process

1. **Establish the target window**: "this coming Thursday through
   Sunday" relative to today's date, same as the arts-roundup skill.
   Confirm with the user if genuinely ambiguous rather than guessing.
2. **Research real, current events for the town(s)** across these
   categories:
   - Special brunches (prix-fixe, themed, holiday-adjacent)
   - Dinner-and-a-show or dinner theater (cabaret seating, BYO
     policies, ticketed dinner packages)
   - Live music at bars and restaurants (named acts/performers,
     recurring weekly series, biergarten/patio shows)
   - Food festivals and one-off events (oyster fests, Oktoberfests,
     beer/wine tastings)
   - Restaurant Week-style multi-restaurant promotions — when one is
     running, don't try to list every participating restaurant; pick
     one or two standout venues and mention the broader promotion for
     context (see the Customization note on this)

   **Exclude drag brunches and any other event centered on drag
   performance**, regardless of how well-verified or otherwise on-topic
   it is. This is a category exclusion, not a verification judgment —
   don't include one even if it's a real, confirmed, on-topic booking
   at an in-town venue.

   Useful source types, roughly in order of reliability:
   - The venue's own site or events page, fetched directly (WebFetch).
     For breweries/venues with a separate ticketing platform (e.g. a
     `turntabletickets.com` or `ticketspice.com` subdomain), that
     platform is usually more current and specific than the venue's own
     homepage — check it directly rather than relying on a homepage
     summary.
   - The town's Patch events calendar (`patch.com/connecticut/<town>/
     calendar?date=YYYY-MM-DD` — the dated URL surfaces far more than
     the undated one, per the arts-roundup skill's finding, which holds
     here too).
   - Town chamber of commerce event pages — **treat with extra caution**:
     these commonly reuse a stable URL for a recurring annual event but
     leave stale details (a past year's dates, a shrunk restaurant list)
     sitting on it uncorrected. Cross-check any chamber page's specific
     dates/numbers against a second, more recent source (a dated Patch
     or local-news article) before trusting it.
   - WebSearch as a discovery tool only, never as final confirmation.
3. **Verify every fact before printing it**, per the same standard as
   the arts-roundup skill: never trust a single search snippet for a
   date, time, address or URL if it can be cross-checked; always fetch
   a URL directly before printing it as a link; cross-check a stated
   day-of-week against the actual calendar date. A generic, undated
   reputation claim ("known for live music on Thursdays," "regularly
   features jazz") is not the same as a confirmed booking for *this*
   weekend — if no specific act/date can be confirmed on a primary
   source, either drop the entry or, if the recurring feature itself is
   well-documented, describe it as a standing weekly feature rather than
   inventing a specific performer for this date (see the Customization
   note on TerraSole).
4. Pick a spread of entries across the window — ideally at least one per
   day — favoring variety across the five categories above over a
   longer list of similar items. A brewery or venue with several strong,
   distinct bookings across different days can appear more than once
   (different acts, different days) without it reading as padding; a
   single generic reputation claim repeated across entries would.
5. Write the article per the format and style rules below.

## Article format

SEO headline, no longer than 109 characters, naming the town and the
draw. **Capitalize the first letter of every word in the headline**,
including short words like "and," "of," "to" and "in" — initial caps
throughout, not standard AP headline case.

Meta description, up to 136 characters, summarizing the article's focus.

One engaging intro paragraph (fun, inviting, accessible tone — no
emoticons) setting up the weekend before the list.

A bulleted list, one entry per event, each including:
- The specific title of the performance, special or promotion
- Day(s) and time(s)
- One to two sentences of real, sourced description — what's included,
  any BYO policy, ticket price or prix-fixe price, age restrictions
  (e.g. 21-and-over festivals)
- The venue name, hyperlinked to the venue's own official website
- The venue's address, hyperlinked to its Google Maps page — see the
  address-format rule below for what text is visible vs. what goes in
  the underlying URL

Closing paragraph, inviting readers to get out and eat, tying back to
the weekend's specific mix of events.

## Style rules

- AP style per this repo's `CLAUDE.md`: no Oxford comma, spell out
  numbers under 10, dollar amounts as `$45` (not `$45.00`), avoid the
  listed AI-isms and "blasted"
- Times as `8 p.m.`, not `8:00 PM`; dates as `Sept. 26` (abbreviated
  only when paired with a specific day number)
- **Venue address format**: the visible/printed address text must NOT
  include the state or ZIP code. Include the town name in the visible
  text only when the venue is located outside the article's target
  town (for a single-town article) or outside the primary town (when
  the user has explicitly widened scope to adjacent towns — see "When
  to use" above).
  - The underlying Google Maps URL should always use the full address
    (street, town, state, ZIP) for accurate geocoding regardless of
    what the visible text shows.
- Hyperlink each venue's Google Maps address using
  `https://www.google.com/maps/search/?api=1&query=<full address,
  URL-encoded>`
- Never fabricate or guess a URL. Every link in the finished article
  must have been fetched and confirmed to resolve during research.

## Delivery

When asked to send the finished article by email, follow the same
disposable-script send mechanism used by the arts-roundup,
recently-sold and death-notices skills: a per-article throwaway Python
script using `smtplib` directly, credentials sourced from
`~/.config/newtown-mail.env` (remember to `set -a` before sourcing it,
since its keys aren't pre-exported), `From: "Patch_Edit_AI
<rich@electricrose.net>"`, sent as HTML (`MIMEText(html_body, "html")`,
since this article's format depends on working hyperlinks), and a line
appended to `beat-archive/send-log.txt`. Subject line convention:
`[Town] restaurant weekend round-up — draft article`. If the user says
"send it to me" rather than naming a recipient, that means their own
address (see the `userEmail` context), not the usual Patch-colleague
recipient a session may otherwise default to.

---

## Customization

Add town-specific or platform-specific findings below this line as this
skill gets used more:

<!-- Your additions here -->

### Ridgefield — first two runs, Sept. 2026

The first run (Ridgefield only) and a same-weekend follow-up (widened
to "Ridgefield or immediately adjacent towns" at the user's request)
together surfaced several reusable findings:

- **Nod Hill Brewery's separate ticketing platform
  (`nod-hill-brewery.turntabletickets.com`) gave a complete, specific,
  dated show schedule** — performer names, times, free-vs-ticketed —
  when the brewery's own homepage only gestured at "several nights a
  week, both free & ticketed shows" without specifics. For any
  brewery/venue that links out to a ticketing subdomain, go there
  directly rather than treating the homepage's vague summary as the
  final word.
- **A same-name-wrong-place trap showed up in this category too, not
  just for arts venues**: "North Star," cited by several sources as a
  Ridgefield restaurant known for Thursday live music, is actually in
  Pound Ridge, New York. Excluded once the state was checked — the same
  discipline the arts-roundup skill documents extensively applies here.
- **A restaurant's own site can confirm the recurring feature (weekly
  brunch hours, a standing live-music night) without confirming a
  specific instance of it** — Baldanza at the Schoolhouse's own site
  confirmed "brunch & dinner Wed-Sun" but not the specific Wilton
  Restaurant Week prix-fixe price/days that Patch had reported. Used
  the confirmed recurring brunch hours in the article rather than the
  unconfirmed promotional price, and said so in an editor's note rather
  than silently substituting one claim for another.
- **A town chamber of commerce's event page can be a stale page reused
  under a permanent-looking URL for an annual event.**
  `wiltonchamber.com/event-detail/wilton-restaurant-week-2-2/` returned
  details for "October 19-25, 2020" — six restaurants, wrong season —
  despite the page presumably being the Chamber's standing URL for this
  recurring event. A dated Patch article
  (`21-restaurants-taking-part-wilton-restaurant-week-what-know`)
  gave the correct current dates (Sept. 21-Oct. 4, 2026) and restaurant
  count (21). When a chamber-of-commerce page's numbers look
  suspiciously round or the season doesn't match, cross-check against a
  dated news article before trusting the chamber page.
- **A well-documented recurring weekly feature without a confirmable
  specific date can still be printed — described as a standing
  feature, not a dated booking.** TerraSole's Thursday/Saturday live
  Italian music is real and repeatedly documented, but no specific
  performer or exact date could be confirmed for this particular
  Thursday. Printed as "Live Italian Music Night — Thursday evening... a
  standing weekly tradition" rather than inventing a performer name or
  dropping the entry outright — this is a genuine middle case between
  "confirmed for this date" and "drop it," distinct from Sarah's Wine
  Bar (dropped entirely, no specific date *or* well-documented standing
  pattern either).
- **A multi-restaurant promotion (Restaurant Week) doesn't fit the
  one-venue-per-entry format cleanly** — resolved by picking one
  representative participating restaurant as the actual list entry and
  mentioning the broader promotion (dates, restaurant count) inside
  that entry's description, rather than trying to list every
  participant or inventing a single "venue" for the promotion itself.

### New Milford — first run, Sept. 2026

A genuinely full week (five entries, Friday through Sunday) that
surfaced one dangerous case worth treating as a standing risk for this
skill specifically, plus two smaller notes:

- **A WebSearch synthesis fabricated a specific booking at a real venue
  by attaching a real touring act's name to it.** A search suggested
  "Street Survivors — Tribute to Lynyrd Skynyrd" was playing at
  Housatonic River Brewing in September 2026; a second, more targeted
  search found several real Street Survivors tour dates (Pennsylvania,
  elsewhere) but none at this venue, and the brewery's own dedicated
  live-music page (`/livemusic-events`) listed two entirely different,
  real, dated acts for this exact weekend (The Outcrops Friday, a Doors
  tribute act Saturday) with no mention of Street Survivors anywhere.
  This is the same failure shape the arts-roundup skill documents
  repeatedly (New Moon Hootenanny, Jan-Ru Wan) — a synthesis blending
  a real touring act with a real local venue into a booking that
  doesn't exist. **For this skill specifically, a brewery/bar's own
  dedicated live-music or events subpage is worth checking before
  trusting any WebSearch-reported specific act, even for a venue
  that's otherwise completely real** — the fabrication risk attaches
  to the specific booking, not the venue.
- **The Fife 'n Drum wrong-town case wasn't a same-state trap like
  North Star/Pound Ridge — it was a neighboring-town mixup**: several
  searches described it as a New Milford live-music restaurant, but
  it's in Kent, the next town over. Since this run's scope was New
  Milford only (no adjacent-towns widening requested), it was excluded
  on the same principle as an out-of-state venue would be — check the
  town, not just the state, even for venues that "feel" local.
- **Not every venue with live entertainment belongs in this skill.**
  Merryall Center for the Arts hosts real, dated live music
  (confirmed via Patch's dated calendar) but is a theater/community
  arts space, not a restaurant or bar — it belongs in the arts-roundup
  skill's performance category, not here. Excluded to keep this
  skill's scope to venues where dining/drinking is the actual business,
  per the skill's own definition at the top of this file.