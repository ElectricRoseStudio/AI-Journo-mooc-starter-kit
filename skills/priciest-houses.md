# Priciest houses skill

Produces a fun, narrative-form hyperlocal news article on the five most
expensive homes currently for sale in a Connecticut town, sourced from
Zillow.com, and emails the result.

## When to use

Invoke this skill when asked for a "most expensive homes" / "priciest
houses" piece for a town, e.g. "do the priciest houses routine for
Waterford" or "write the top 5 most expensive homes for sale in New Milford."
Unlike the employment/gas-prices skills, each town gets its own separate
top-5 list even when several towns share a PATCHBEAT — a beat's towns aren't
combined into one shared ranking here, since the ask is specifically "the
five most expensive in [town]," not a beat-wide figure.

## Process

1. **Build a candidate pool.** `zillow.com`'s own search/sort pages are
   unreliable for this: `?sort_amount-high` and pretty-URL price-range
   filters (e.g. `/ridgefield-ct/1500000-99000000_price/`) are both silently
   ignored when fetched — the same ~9 unsorted, unfiltered listings come back
   regardless of the sort/filter requested. Don't trust that a "sorted" fetch
   is actually sorted.

   The working source for a broad candidate list is
   `https://www.compass.com/homes-for-sale/{town-slug}-ct/` via plain
   WebFetch (no proxy needed, unlike Zillow) — ask it to list every home
   shown with address/price/sqft/beds/baths and to name the 5-6 highest by
   price. This reliably returns dozens of real addresses and a rough price
   ranking, but the price/detail figures on it are frequently stale (see
   next step) — it's a lead-generation source, not a final source.

2. **Verify every single candidate individually before using it.** This is
   the most important step. Both Zillow's own cached/indexed listing pages
   and Compass's town page produce a high rate of false positives — in two
   towns' worth of runs this session, roughly 8 of the first ~15 high-price
   candidates checked turned out to be off-market, sold, or otherwise not
   actually purchasable, despite looking "for sale" in search results or
   aggregate listings. Do not include a price/detail figure in the article
   without having fetched that specific property's own Zillow page and
   confirmed its status.

   To fetch a Zillow property page (direct WebFetch/curl both 403 on every
   Zillow endpoint tried, including the bare homepage — same Cloudflare-style
   block documented in the gas-prices skill for GasBuddy):
   - If you have a zpid (from a WebSearch result, e.g.
     `site:zillow.com/homedetails "{street}" {Town} CT`), fetch
     `https://r.jina.ai/https://www.zillow.com/homedetails/{Street-Slug}-{Town}-CT-{zip}/{zpid}_zpid/`.
   - If you only have the address, `https://r.jina.ai/https://www.zillow.com/homes/{Street-Slug}-{Town}-CT-{zip}_rb/`
     works directly and resolves Zillow's own address search — no need to
     hunt for a zpid first. This was the faster, more reliable path this
     session; prefer it over searching for a zpid.
   - Ask explicitly for the status badge at the top of the page ("For sale",
     "Pending", "Off market", "Sold", "Coming soon [date]", "Accepting
     backups") plus price, sqft, bedrooms, bathrooms (full/half), and days
     on Zillow. Quote numbers exactly rather than letting the summary round
     or infer.

   Exclude anything that isn't a plain, current "For sale":
   - **Off market / Sold** — not purchasable, exclude outright.
   - **Pending / Under contract / Accepting backups** — already spoken for;
     exclude even though Zillow still shows a price. (Established this
     session on 43 Catoonah St and 26 Lynn Place, Ridgefield — both
     genuinely active-looking in search results, both actually pending.)
   - **Coming soon [date]** — not yet listed/available to tour or offer on;
     exclude even though it has a price and specs. (40 Beechwood Ln,
     Ridgefield; 186 Great Neck Rd and 8 White Oak Ln, Waterford; 54
     Pleasant Valley Rd, Clinton — all "Coming soon" this session.)
   - **New construction** with an active "For sale"/build-to-order status
     is fine to include (it's purchasable now, just not yet built) — don't
     confuse this with "Coming soon."

   If a fetched page is missing a required field (e.g. bathroom count not
   stated anywhere on the page — happened with 274 Joshuatown Rd, Lyme),
   don't borrow the number from a different source (Compass, a WebSearch
   snippet) to fill the gap — drop that candidate and use the next-highest
   verified one instead. Two independent numbers for the same address that
   disagree (seen repeatedly: search snippets citing stale historical
   prices, or details belonging to a different nearby address entirely —
   e.g. a "263 Peaceable St" search summary that was actually reciting
   463 N Salem Rd's specs) should never both make it into the article;
   trust only the number from that property's own direct Zillow fetch.

3. **Rank and select the top 5** by confirmed current asking price, highest
   first. Keep checking further down the Compass candidate list (or search
   for more candidates) until 5 genuinely active listings are confirmed —
   expect to check 8-12 candidates per town to land 5 clean ones.

4. **Write the article** using the format below.

5. **Email it** using the project's SendGrid pipeline (see the gas-prices
   skill for the credential/verification pattern — same `send_*` script
   shape, same SendGrid Activity API delivery check). For a multi-town
   batch, one combined email with each town's article under an `<h2>`
   heading plus all files individually attached, matching prior sends.

6. **Save to `beat-archive/`** only if asked (git-ignored by design).
   Filename pattern: `{town-slug}-most-expensive-homes-{mon}{yyyy}.html`.

## Article format

Fun, narrative tone (not the dry AP-style register used for the
employment/gas-prices pieces) but still AP style per this repo's
`CLAUDE.md` — no Oxford comma, no editorializing beyond light color writing.

1. Headline — every single word capitalized, no exceptions for minor words
   ("Take A Tour: Town's Five Priciest Homes On The Market Right Now" —
   "A," "On," "The" all capitalized too). This is stricter than normal
   AP/title-case headline style; confirm before reusing a headline pattern
   that it satisfies "every word," not just "major words."
2. Dateline lead paragraph: town-specific hook, credit
   `<a href="https://www.zillow.com/homes/">Zillow.com</a>` — every time
   "Zillow.com" is printed anywhere in the piece, it's this exact hyperlink.
3. One narrative paragraph per property, ranked 1-5 ("No. 1 on the list
   is...", "Just behind it...", "Rounding out the top three...", "No.
   4...", "Closing out the top five..."). Each paragraph must include, in
   prose rather than a bullet list: overall size (sqft), asking price, days
   on the market, bedroom count, bathroom count. Weave in whatever genuine
   descriptive detail was found (architectural style, notable history,
   waterfront features, renovations) — don't invent color that wasn't in a
   verified source; a plain spec-focused paragraph is better than a
   fabricated one.
   - Address is a hyperlink to Google Maps
     (`https://www.google.com/maps/search/?api=1&query={URL-encoded "street address, Town, CT"}`),
     with the **town name omitted from the visible link text** (the query
     param itself still includes town+state for accuracy).
   - Abbreviate Ave., Blvd., St. only when paired with a house number
     (e.g. "152 High Ridge Ave.," "11 Stanton St.") — every other street
     suffix (Road, Lane, Drive, Circle, Trail, Place, etc.) stays spelled
     out; this project has had far more Road/Drive/Lane addresses than
     Ave./St. ones, so don't over-apply the abbreviation by habit.
   - If a street name is itself a number, spell out First through Ninth
     and use figures for 10th and up (hasn't come up yet in any town
     checked so far, but watch for it).
   - Abbreviate Jan., Feb., Aug., Sept., Oct., Nov., Dec. only when paired
     with a specific date (e.g. "Aug. 27," "price cut... on Aug. 26");
     spell out when standalone or with only a year, and never abbreviate
     Mar./Apr./May/Jun./Jul. at all. Don't manufacture a specific calendar
     date by subtracting "days on Zillow" from today just to have an
     abbreviation to use — only print a specific date when the source
     itself stated one (a listed-on date, a price-cut date, an open-house
     date); otherwise state days-on-market as a plain duration.
4. Closing paragraph: sourcing/freshness disclaimer, dated (with the
   Aug./Sept./etc. abbreviation rule applied to that date too).

## Customization

Add town-specific or technique additions below this line:

<!-- Your additions here -->

### Parallelizing across multiple towns: r.jina.ai rate-limits under load

Confirmed 2026-08-27, running 8 towns as parallel fork agents at once
(Danbury, Newtown, Bethel, Monroe, Southbury, Brookfield, Wilton, Weston):
the `r.jina.ai` proxy started returning HTTP 429 partway through for at
least two of the eight forks, almost certainly from several forks hitting
the same proxy concurrently from the same environment/IP. Both forks
recovered by pacing requests — one switched from the WebFetch tool to raw
`curl` with `sleep 15` between calls, the other paced sequentially rather
than firing verification fetches back-to-back. If running this skill for
several towns at once (e.g. via parallel fork agents), expect this and
build in spacing between `r.jina.ai` fetches rather than firing them as
fast as possible; a single-town run hasn't hit this limit.
