# Gas prices skill

Produces a hyperlocal AP-style gas price article for a Connecticut town (or a
combined multi-town beat), comparing the town's current station prices against
the Connecticut state average and the U.S. national average, and emails the
result.

## When to use

Invoke this skill when asked to write/update a gas price article for a town or
beat, e.g. "do the gas prices routine for Waterford" or "create gas price
articles for New Milford, Orange, Clinton and the Lyme-Old Lyme-East Lyme
beat." A beat with more than one town (per `CT_Towns.csv`'s `PATCHBEAT`
column, semicolon-separated) gets one combined article, not one per town —
same rule as the death-notices/employment routines for multi-town beats.

## Process

1. **State average — AAA.** Fetch `https://gasprices.aaa.com/?state=CT` via
   WebFetch. Extract the current Connecticut regular-gas average, plus the
   one-week-ago, one-month-ago, and one-year-ago averages, and the "as of"
   date shown on the page. WebFetch has worked directly on this site (no
   curl/browser workaround needed).

2. **National and regional average — EIA.** Fetch
   `https://www.eia.gov/petroleum/gasdiesel/` via WebFetch for the current
   U.S. national regular-gas average and its release/data date. EIA does
   **not** publish a Connecticut-specific figure — it only tracks Connecticut
   as part of "New England (PADD1A)." Use that regional figure as the
   disclosed EIA proxy for Connecticut when a requirement calls for an
   EIA-sourced CT number; state plainly in the article that EIA doesn't track
   CT individually. Don't substitute AAA's CT number where EIA is specifically
   requested, or vice versa — they're different comparisons or the requirement
   will be violated even though the numbers are close.

3. **Station-level prices — GasBuddy.** `gasbuddy.com` blocks both curl (even
   with a full browser User-Agent) and WebFetch directly — confirmed 403 on
   every endpoint tried, including the bare homepage. The browser extension
   (claude-in-chrome) is the "real" workaround but is frequently not connected
   in this environment. The working fallback that got through repeatedly this
   session: prefix the target URL with the `r.jina.ai` read-only proxy and
   fetch that through WebFetch, e.g.
   `https://r.jina.ai/https://www.gasbuddy.com/gasprices/connecticut/{town-slug}`.
   This reliably returns real station names, addresses, and prices.

   - The town-slug page (`gasbuddy.com/gasprices/connecticut/{slug}`) is a
     "top N" list, not exhaustive — it typically shows 5-10 stations, some
     with a price, some showing `- - -` (no current community report). Only
     use stations that show a real price; do not fabricate a price for a
     dashed-out station. Note in the article that not every station in town
     had a current report.
   - If the `r.jina.ai` fetch returns thin/incomplete data (e.g. a price
     shown on the aggregate list doesn't match what the individual station
     page shows), prefer the **individual station page**
     (`r.jina.ai/https://www.gasbuddy.com/station/{id}`) as more authoritative
     — it also shows how recently the price was reported (e.g. "2 Hours
     Ago"), which the aggregate list doesn't always surface. Re-fetch once
     with a `?t=N` cache-busting query param on the `r.jina.ai` URL if a
     first attempt comes back oddly stale or empty — this proxy appears to
     cache responses.
   - Some towns don't have a working `gasbuddy.com/gasprices/connecticut/{slug}`
     page at all (404) — small/rural towns may genuinely have no tracked gas
     stations (confirmed for Lyme, CT). Don't force a result; state plainly in
     the article that no stations were found for that town.
   - To find a station's numeric GasBuddy ID (needed for both re-fetching its
     individual page and for the final article's price hyperlink) when the
     aggregate listing doesn't supply one, use WebSearch:
     `site:gasbuddy.com station "{Town}, CT" {street name}`. This reliably
     surfaces `gasbuddy.com/station/{id}` links with name/address in the
     result titles, and works well for pulling several stations' IDs in one
     query.
   - Double-check every station's address is actually in the target town —
     a town's GasBuddy listing sometimes includes a bordering town's station
     (e.g. Clinton's list included a Madison-addressed Mobil).

4. **Compute the town's local average.** Average only the stations with a
   currently reported price (simple mean, no weighting). For a multi-town
   beat, compute and state each town's own average **separately** — do not
   sum/blend across towns into one regional number unless explicitly asked;
   see the multi-town format note below.

5. **Write the article** using the format and constraints below.

6. **Email it** using the project's existing SendGrid pipeline (see
   `scripts/send-clinton-docs.py` for the credential/SMTP pattern; source
   `~/.config/newtown-mail.env` for `SMTP_HOST`/`SMTP_PORT`/`SMTP_USER`/
   `SMTP_PASS`/`SMTP_FROM`). Send the article as HTML — both as the email
   body (`MIMEText(html, "html")`, so links render clickable) and as an
   attached `.html` file. For a batch of several towns in one request, one
   combined email with each article's HTML concatenated under a per-town
   `<h2>` heading, plus all files individually attached, matches what's been
   sent previously.

7. **Verify delivery** via SendGrid's Email Activity API rather than trusting
   the SMTP "sent" response alone — this project's established practice after
   past incidents where local success signals didn't reflect actual delivery.
   Query `https://api.sendgrid.com/v3/messages?limit=1&query={URL-encoded
   to_email="rich.kirby@patch.com"}` with `Authorization: Bearer $SMTP_PASS`
   (the SendGrid API key doubles as `SMTP_PASS`). The Activity API has
   indexing lag (seen up to ~90 seconds this session) — poll every 6-8
   seconds rather than checking once and assuming failure.

8. **Save to `beat-archive/`** only if asked (this repo's `beat-archive/` is
   git-ignored by design — confirmed with the user it should stay that way).
   Use filename pattern `{town-or-beat-slug}-gas-prices-{mon}{yyyy}.html`,
   matching the existing `{town}-employment-{month}{year}.txt` convention
   from the employment-data skill/routine.

## Article format

Structure (AP style per this repo's `CLAUDE.md` — no Oxford comma, no
editorializing):

1. `Headline: {headline}` — title case, ≤109 characters.
2. `Meta description: {description}` — ≤136 characters.
3. Lead paragraph: dateline (`TOWN, Conn. — `), current town context vs.
   state/national trend, crediting AAA and EIA.
4. State paragraph: current CT average (AAA) vs. week/month/year ago, noting
   increase/decrease and the dollar/cent difference.
5. State-vs-US paragraph: CT average (AAA) vs. US average (EIA).
6. EIA-CT-proxy paragraph: disclose EIA tracks CT only via the New England
   region; give that regional figure.
7. Town paragraph: the town's (or, for a multi-town beat, each town's own)
   average vs. the CT average, the EIA New England figure, and the EIA US
   figure — state cents above/below for each comparison.
8. Short transition sentence introducing the station list.
9. Bulleted list — required hyperlinks only, no other bold/italic
   formatting:
   - `Station Name, [street address, no town name](Google Maps search link):
     [$X.XX](GasBuddy station URL)`
   - Google Maps link format:
     `https://www.google.com/maps/search/?api=1&query={URL-encoded "address, Town, CT"}`
   - Never write "GasBuddy" (or name the source at all) in the visible
     article text — only "a crowdsourced fuel-price tracking site" if a
     source needs naming in prose (e.g. the sources footer).
   - For a multi-town beat, tag each bullet with its town in parentheses
     after the station name, since addresses alone don't make the town
     obvious across a combined list.
10. Disclaimer paragraph: prices fluctuate, figures reflect time of
    publication.
11. Sources footer: AAA and EIA both credited **as hyperlinked acronyms**
    (`<a href="https://gasprices.aaa.com/">AAA</a>`,
    `<a href="https://www.eia.gov/petroleum/gasdiesel/">EIA</a>`) — no
    separate standalone URL text for either. Every AAA/EIA mention anywhere
    in the article, not just the footer, should be the hyperlinked acronym.

### Multi-town beat format

Same lesson as the employment-data routine's multi-town format (learned the
hard way — see conversation history): **do not** blend towns into one
combined/summed average unless explicitly asked. Present each town's own
average in one flowing paragraph (not separate headed sub-sections per town),
e.g. "Old Lyme's station with current reports averaged $4.19 a gallon...
East Lyme's stations with current reports averaged $3.97 a gallon..." — same
sentence-per-town pattern, not three separate `<h3>`-style blocks.

## Customization

Add town-specific or technique additions below this line:

<!-- Your additions here -->
