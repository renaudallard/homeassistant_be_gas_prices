# The provider framework

How a supplier module plugs in, and the shared pieces it builds on. Read
[architecture.md](architecture.md) first; the reference implementation is
`providers/engie.py` with `tests/test_engie.py`.

## The protocol

A supplier module exposes `EXTRACTOR: SupplierExtractor` (`providers/base.py`):

| Field | What it is |
| --- | --- |
| `id`, `label` | The registry id and the name the setup flow shows. |
| `contracts` | `Contract(id, label, kind, regions, professional)` for each product sold. `regions` is where the product is actually published. |
| `fetch(session, contract, region)` | The current card as a `SupplierSnapshot`. Raises `ExtractorError` on anything mandatory it cannot read; never defaults a missing figure to zero. |
| `probe(session, contract, region)` | Optional. A cheap freshness key; the same key means the card is unchanged. |
| `fetch_for_month(session, contract, region, month)` | Optional. The card published for a past month, None where the supplier serves none, `ExtractorError` only on a failure that may recover (`is_transient_fetch_error`). Refuses a card that names another month, except a card in force until the next one (Bolt's variable cards, `valid_until` None), which names the month it was first published for. Uses Home Assistant's clock. `_validity.month_card` does all but the lookup. |
| `fetch_index(session)` | Optional. The supplier's own index publication as `{index name: {"YYYY-MM": EUR/MWh}}`. |
| `deprecated_until`, `deprecated_successor` | A supplier leaving the market. |
| `sweep_cost_s` | Roughly what one card costs to fetch and parse, for the comparison's scheduling. |
| `withdrawn` | The labels of the contracts the supplier withdrew, by id: an entry naming one fetches nothing and keeps pricing on the last card it holds, from its store or the card archive, under a `contract_withdrawn` Repairs card, and its wizard title and its row in a comparison keep the label (`contract_label`). |
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

## Conventions

- Figures as the card prints them, converted to EUR. A residential card is VAT
  inclusive (`vat_rate` 0.0); a formula printed excluding VAT is grossed up by
  the card's own printed rate, or the residential 6 % where the card prints
  none (Energy Together, Bolt, energie.be, OCTA+), and one already carrying
  "x 1,06" is not grossed again.
- `IndexedRates.price` is the card's figure at the last known index where it
  prints one, else its printed estimate. `index` names the supplier's own
  series, the key of its `fetch_index` table.
- Levies are read as printed, stale or not: the law's figures for the
  delivery month are applied later (`_resolve.resolve_for_delivery`).
- A card from the wrong product (Luminus serves another product's gas card
  for an electricity-only slug) is refused on the product name it prints.

## Shared readers

| Module | Helpers |
| --- | --- |
| `_pdf.py` | HTTP and PDF: `fetch_text`, `fetch_pdf_text` (pypdf), `fetch_pdf_text_layout` (pdfplumber), `fetch_pdf_rendered` and `pdfplumber_text` for an extractor's own render, `word_rows` and `word_centre` for one working from pdfplumber's words, `render_pdf` for bytes obtained otherwise, `parse_json`, `MONTH_NAMES` and `NL_MONTHS`, the transient-error taxonomy, `read_capped` and `read_text_capped`, which hold every body to 64 MiB as it streams, `guard_redirect`, which refuses an answer a redirect carried off https (not off the site: TotalEnergies and energie.be serve their index publications from another host), the text-layer check (`CardNotReadableError`), the per-walk text memo and the render hook the card archive listens on. |
| `_parse.py` | `to_float` (comma or dot decimals, thousands dots beside a comma), `split_row` and `table_row` (a table row by fuzzy label, cells read from the right so a label like "TECTEO - RESA" keeps its dash), `fold_accents`, `month_date` (the month a card names, an `ExtractorError` when the figures are no month, such as a year 0000), `require_contract`, `html_rows` and `html_cells` for an HTML table. |
| `_network.py` | `read_dsos(text, labels, columns, ...)` with the column roles named in the card's header order, collapsing ORES's five sub-area rows (which must agree) and dropping a tier that is not degressive with those above it (a figure in the wrong cell), `require_region`, `excise_bands`, `osp_table` for the Brussels levy amounts in row order. Shared label maps for the Fluvius and ORES rows. |
| `_validity.py` | `end_of_month`; for `fetch_for_month`, `future_month` (Home Assistant's clock) and `month_card`, which wraps the month's load: a transient failure raises, any other failure or a card naming another month is None. |
| `_resolve.py` | Per household and delivery month: `tier_for`, `effective_excise`, `resolve_for_delivery`, `brussels_levy`. |

## Tests

Real cards only, under `tests/fixtures/<supplier>/`. A provider's tests pin
the figures read off the card per region and product kind, one full DSO row
per region, the levies, and the formula check: the formula at the index value
the card names reproduces its printed price. `fetch_for_month` and
`fetch_index` are tested offline with the fetch helpers mocked and the clock
frozen.
