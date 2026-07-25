# ChatGPT business registry template

Reference templates for running the "new business roundup" workflow in ChatGPT
instead of Claude. Use these when you need to hand the CT Business Registry →
Datawrapper table → hyperlocal article pipeline to someone using ChatGPT, or
when you're testing how the same task performs across tools.

## Why this isn't a straight copy-paste of the Claude prompt

- **Live data fetch** — Claude Code can hit the Socrata REST endpoint
  directly and pull real JSON. Standard ChatGPT can only do this if its web
  tool fetches raw JSON URLs; if it can't, the data has to be pasted in
  manually.
- **Datawrapper API calls** — Claude can call the Datawrapper API directly
  with a key. Plain ChatGPT chat cannot call an external authenticated API on
  its own — that only works inside a Custom GPT with an Action (OpenAPI
  schema) wired to Datawrapper. Without that, ChatGPT can only hand back a
  CSV/table plus manual build instructions.
- **API keys** — never paste a live API key into a ChatGPT prompt. It gets
  sent to OpenAI's servers and, if the prompt is reused as a template,
  exposed to everyone who copies it. Put the key only in a Custom GPT
  Action's auth config, never in chat text.

## Version A — Custom GPT with Actions for Socrata + Datawrapper

Use this if you've set up a Custom GPT with an Action wired to the
Datawrapper API (auth handled by the Action config, not in the prompt).

```
Query the Connecticut Business Registry via the Socrata API endpoint
https://data.ct.gov/resource/n7gp-d28j.json. Retrieve every business
with Business_City equal to [PATCHTOWN] (case-insensitive), Business_State
equal to CT, and Date_Registration between [EARLIEST DATE (MMMM DD, YYYY)]
and [LATEST DATE (MMMM DD, YYYY)].

Build a table with three columns: Business (from Name), Address (from
Business_Street), and Type (from NAICS_Code, with the parenthesized
numeric code stripped, keeping only the descriptive category text).

Using the Datawrapper Action, create a table chart from this data.
Title it "[PATCHTOWN] Businesses Registered in [MMM YYYY]." Apply row
stripes, enable Mobile Fallback and Compact Layout, leave the
Description field blank, and show all records on one page.

Then write a 2-3 paragraph hyperlocal new-business article for a
[PATCHTOWN] audience, using the table as the centerpiece. The audience
already knows [PATCHTOWN] is in Connecticut, so do not include the
state name when referencing the town.

Finally, write:
- An SEO-optimized headline (≤109 characters, Title Case) that spotlights
  one business with an unusual category
- A meta description (<136 characters)
```

## Version B — standard ChatGPT (no Actions)

Use this for a plain ChatGPT session. It skips automated Datawrapper
creation and instead has ChatGPT produce a ready-to-paste CSV plus manual
build instructions.

```
Fetch this URL and use the returned JSON as your data source:
https://data.ct.gov/resource/n7gp-d28j.json?$select=name,billingstreet,naics_code,billingcity,billingstate,date_registration&$where=upper(billingcity)='[PATCHTOWN, ALL CAPS]' AND billingstate='CT' AND date_registration between '[EARLIEST DATE, YYYY-MM-DD]T00:00:00' and '[LATEST DATE, YYYY-MM-DD]T23:59:59'&$limit=5000

If you cannot fetch URLs directly, ask me to paste the JSON before continuing.

Build a table with columns Business (name), Address (billingstreet), and
Type (naics_code with the trailing parenthesized NAICS number removed —
keep the descriptive category text, including any parentheses that are
part of the category name itself, e.g. "Offices of Mental Health
Practitioners (except Physicians)").

Output that table as:
1. A Markdown table
2. A CSV code block, ready to paste into Datawrapper

Then give me the exact settings for building this as a Datawrapper table
chart, since you can't create it directly: title
"[PATCHTOWN] Businesses Registered in [MMM YYYY]," row stripes on,
Mobile Fallback on, Compact Layout on, Description left blank, all rows
shown on one page.

Then write a 2-3 paragraph hyperlocal new-business article for a
[PATCHTOWN] audience using the table as the centerpiece. The audience
already knows [PATCHTOWN] is in Connecticut, so don't include the state
name when referencing the town.

Finally give me:
- An SEO-optimized headline (≤109 characters, Title Case) spotlighting
  one business with an unusual category
- A meta description (<136 characters)
```

## Placeholders

- `[PATCHTOWN]` — town name as it should appear in prose (e.g. `Ridgefield`)
- `[PATCHTOWN, ALL CAPS]` — town name as stored in `billingcity` (e.g. `RIDGEFIELD`)
- `[EARLIEST DATE]` / `[LATEST DATE]` — registration window bounds
- `[MMM YYYY]` — month/year for the table title (e.g. `June 2026`)
