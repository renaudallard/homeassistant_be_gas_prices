# The provider framework

How a supplier module plugs in, and the shared pieces it builds on. Read
[architecture.md](architecture.md) first; the reference implementation is
`providers/engie.py` with `tests/test_engie.py`.

## The protocol

A supplier module exposes `EXTRACTOR: SupplierExtractor` (`providers/base.py`):

| Field | What it is |
| --- | --- |
| `id`, `label` | The registry id and the name the setup flow shows. |
| `contracts` | `Contract(id, label, kind, regions, professional, withdrawn)` for each product sold, and each one withdrawn while households may still be on it. `regions` is where the product is actually published. `withdrawn` is the first day it was no longer sold: the setup offers it marked †, the comparisons and the daily card walk leave it out, the archive's backfill still mirrors its months, and an entry naming one fetches nothing and keeps pricing on the last card it holds, from its store, else the card of the last month it was sold, from the card archive or `fetch_for_month`, under a `contract_withdrawn` Repairs card. |
| `fetch(session, contract, region)` | The current card as a `SupplierSnapshot`. Raises `ExtractorError` on anything mandatory it cannot read; never defaults a missing figure to zero. |
| `probe(session, contract, region)` | Optional. A cheap freshness key; the same key means the card is unchanged. |
| `fetch_for_month(session, contract, region, month)` | Optional. The card published for a past month, None where the supplier serves none, `ExtractorError` only on a failure that may recover (`is_transient_fetch_error`). Refuses a card that names another month, except a card in force until the next one (Bolt's variable cards, `valid_until` None), which names the month it was first published for. Uses Home Assistant's clock. `_validity.month_card` does all but the lookup. |
| `fetch_index(session)` | Optional. The supplier's own index publication as `{index name: {"YYYY-MM": EUR/MWh}}`. |
| `discover(session)` | Optional. The products the supplier lists as sold today, a registered contract by its id and anything else by an id naming it, which the live check holds against `contracts`. Frank's reads its terms page. |
| `deprecated_until`, `deprecated_successor` | A supplier leaving the market. |
| `sweep_cost_s` | Roughly what one card costs to fetch and parse, for the comparison's scheduling. |
| `images_only` | The supplier publishes its cards as page images, which only the card archive's reading prices: the flow requires the archive, and an entry without it stops at setup. |

Each module also has a pure `parse_snapshot(contract, region, text, ...)`
that the tests call on a fixture's text.

Every caller awaits `fetch`, `fetch_for_month` and `fetch_index` through
`_pdf.guarded`, which turns any other exception into an `ExtractorError`
naming its class, with the traceback logged, so a parser bug fails like a
card the parser refuses and the held card keeps pricing.

## The snapshot

`SupplierSnapshot` holds:

- `energy`: `FixedRates(price, yearly_fixed_fee)`,
  `VariableRates(price, yearly_fixed_fee, formula)` or
  `IndexedRates(factor, base, index, price, yearly_fixed_fee, formula,
  settled, period)`. All EUR/kWh with the index in EUR/MWh.
- `dsos`: `{dso key: DsoOverlay(tiers, transport, metering_per_year)}`, the
  tiers keyed `t1`, `t2` and where printed `t3`, each
  `DsoTier(fixed_per_year, proportional)`.
- `taxes`: `TaxOverlay(excise_bands, energy_contribution, connection_fee,
  osp_by_caliber, vat_rate, card_vat_rate)`.
- `publication_label` ("YYYY-MM", the card's month), `valid_until` (the last
  day of it, or None for a card in force until the supplier replaces it, as
  Bolt's variable cards are) and `source_url`.
- `fee_waived_months`: the months from its start a contract signed on the
  card pays no supplier fee for, 0 for most cards; the energy leg's
  `yearly_fixed_fee` is the fee billed after them. Frank's November 2026 JN
  card waives twelve.

## Conventions

- Figures as the card prints them, converted to EUR. A residential card is VAT
  inclusive (`vat_rate` 0.0); a formula printed excluding VAT is grossed up by
  the card's own printed rate, or the residential 6 % where the card prints
  none (Energy Together, Bolt, energie.be, OCTA+), naming the fields it
  grossed in `IndexedRates.vat_grossed` for the month billed to move onto
  its own rate, and one already carrying "x 1,06" is not grossed again.
- `IndexedRates.price` is the card's figure at the last known index where it
  prints one, else its printed estimate. `index` names the supplier's own
  series, the key of its `fetch_index` table.
- Levies are read as printed, stale or not: the law's figures for the
  delivery month are applied later (`_resolve.resolve_for_delivery`). A
  single excise rate too high for gas (EnergyVision's Walloon fixed card
  prints the electricity one) is not read at all: the card holds no gas
  excise, and its months are priced on the law's alone.
- A card from the wrong product (Luminus serves another product's gas card
  for an electricity-only slug) is refused on the product name it prints.

## Shared readers

| Module | Helpers |
| --- | --- |
| `_pdf.py` | HTTP and PDF: `fetch_text`, `fetch_pdf_text` (pypdf), `fetch_pdf_text_layout` (pdfplumber), `fetch_pdf_rendered` for an extractor's own render, `render_pdf` for bytes obtained otherwise, `parse_json`, `MONTH_NAMES` and `NL_MONTHS`, the transient-error taxonomy, `read_capped` and `read_text_capped`, which hold every body to 64 MiB as it streams, `guard_redirect`, which refuses an answer a redirect carried off https (not off the site: TotalEnergies and energie.be serve their index publications from another host), the per-walk text memo and the render hook the card archive listens on. |
| `_pdf_render.py` | Turning a card's bytes into text: `extract_pdf_text` (pypdf), `extract_pdf_text_layout` (pdfplumber), `extract_pdf_text_rows` (pdfplumber's words in rows, for OCTA+'s older template), `pdfplumber_text` for an extractor's own render, `word_rows` and `word_centre` for one working from pdfplumber's words, and the text-layer check (`CardNotReadableError`). The card archive stamps every stored text with a digest of this module (`scripts/card_texts.py`), so only a change here renders the kept cards again. |
| `_parse.py` | `to_float` (comma or dot decimals, thousands dots beside a comma), `split_row` and `table_row` (a table row by fuzzy label, cells read from the right so a label like "TECTEO - RESA" keeps its dash), `fold_accents`, `month_date` (the month a card names, an `ExtractorError` when the figures are no month, such as a year 0000), `require_contract`, `html_rows` and `html_cells` for an HTML table. |
| `_network.py` | `read_dsos(text, labels, columns, ...)` with the column roles named in the card's header order, collapsing ORES's five sub-area rows (which must agree) and dropping a tier that is not degressive with those above it (a figure in the wrong cell), `require_region`, `signing_dsos` with `FLUVIUS_MERGED` for a card from before a supplier's current table layout (its energy leg alone, no DSO row), `excise_bands`, `osp_table` for the Brussels levy amounts in row order. Shared label maps for the Fluvius and ORES rows. |
| `_validity.py` | `end_of_month`; for `fetch_for_month`, `future_month` (Home Assistant's clock) and `month_card`, which wraps the month's load: a transient failure raises, any other failure or a card naming another month is None. |
| `_resolve.py` | Per household and delivery month: `tier_for`, `effective_excise`, `resolve_for_delivery`, `brussels_levy`. |

## A card that moved (providers/_moved.py)

`EXTRACTORS` wraps the `fetch` of Elegant, Engie and Dots
(`with_moved_fallbacks`). When the usual address answers that it has no card
(`is_missing_card_error`: a 404 or 410, or a web page in its place), when its
listing names none (`CardNotListedError`, which Elegant raises when its
listing links no card for the product), or with a card of a month before the
running one, the wrapper asks the supplier's own pages for the product's card
of the running month (`FALLBACKS`: Elegant's listing by the archive key,
Engie's prices pages by family, rate and region, Dots's product page) and
reads each address with the module's `_read`, the read step its `fetch`
uses. The first card whose `valid_until` is the running month's end is kept
and the address logged; with none the usual answer stands, the error or the
older card. A parse error or a network failure is no sign of a move and is
never searched for. An Engie sibling under the same family code is offered
by the candidates but marked, and the wrapper skips it.

Luminus, Belvus and EnergyVision are not wrapped. Luminus's `fetch` reads its
archive's card of the running month when the usual address has none,
Belvus's reads both pages that list its cards, and EnergyVision's asks every
folder its listing's script asks, newest upload first, where its card page
linked the first edition of a card a corrected upload had replaced in
November 2026.

The wrapper keeps the module's own `fetch` as `__wrapped__`, with its name
and module (`functools.wraps`): the live check asks
`inspect.unwrap(ex.fetch)`, so it keeps failing at the usual address until
the extractor reads the card there, and counts each supplier's figures once
by its module. `scripts/card_finder.py` reports from the same candidates
where the card went, the marked ones included. The card archive reads the
registry, so it files a moved card at the month it names.

## Tests

Real cards only, under `tests/fixtures/<supplier>/`. A provider's tests pin
the figures read off the card per region and product kind, one full DSO row
per region, the levies, and the formula check: the formula at the index value
the card names reproduces its printed price. `fetch_for_month` and
`fetch_index` are tested offline with the fetch helpers mocked and the clock
frozen.
