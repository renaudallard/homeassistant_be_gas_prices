# Eneco

Module: `custom_components/be_gas_prices/providers/eneco.py`. Tests:
`tests/test_eneco.py`, fixtures in `tests/fixtures/eneco/`.

## Where the cards are

- Listing: https://eneco.be/nl/elektriciteit-gas/tariefkaarten/ (the path
  without the trailing slash answers a 308 to it). It links every current
  card, gas and electricity, as a full URL.
- Card: `https://cdn.eneco.be/downloads/nl/general/tk/BC_032_<ISSUE>_NL_ENECO_GAS_<TOKEN>.pdf`.
  `ISSUE` is `01` and the card's `YYMM` (`012609` for September 2026); the
  card repeats it as "Tariefkaart versie 012609".
- `fetch` reads the listing and takes the first link matching the product's
  token, closed by `.pdf` so `FLEX` never matches `FLEX_ONE` (`card_url`).
- One Dutch card serves Flanders and Wallonia. A French card exists under
  `/downloads/fr/` and is not read.
- No probe: the listing sends `Cache-Control: no-store` and a new ETag on
  every request, and the card URL is only known from the listing.

## Products and regions

| Contract id | Label (card title) | Token | Kind | Regions |
|---|---|---|---|---|
| `eneco_aardgas_vast` | Eneco Aardgas Vast | FIX | fixed | flanders, wallonia |
| `eneco_aardgas_flex` | Eneco Aardgas Flex | FLEX | indexed, monthly | flanders, wallonia |
| `eneco_aardgas_flex_one` | Eneco Aardgas Flex One | FLEX_ONE | indexed, monthly | flanders, wallonia |

- No Brussels: Brugel's supplier list notes "Eneco ne propose actuellement
  plus de contrat aux nouveaux clients résidentiels bruxellois", and the cards
  say "voor Vlaanderen en Wallonië".
- Vast is "vast tijdens het eerste jaar" and "geldig binnen de grenzen van de
  beschikbare volumes": Eneco may stop selling it before the month ends.
- Flex is open-ended, Flex One "een contract van 1 jaar". Same layout, factor
  and fee; only the base differs.
- The parser refuses a card whose first line is not the contract's label,
  since Flex and Flex One are otherwise indistinguishable.

## How each figure is read (pypdf text)

- Card month: "Tariefkaart september 2026 van Eneco". `publication_label`
  and `valid_until` (end of that month) come from it.
- Energy, under "VERBRUIK (€cent/kWh)":
  - Vast: "65,00 9,13" on one line, yearly fee then price.
  - Flex and Flex One: the fee alone ("65,00"), then "7,76 Geschatte
    jaarprijs", then "7,67 Maandprijs". `price` is the Maandprijs. Cards
    before 2025 put a ">" before each label; both forms are read.
  - Formula: "(0,1 X TTFDAW-RLP-M + 1,074) X 1,06". The multiplier is the
    VAT and is applied once: factor = 0,1 x 1,06 / 100, base = 1,074 x 1,06 /
    100 (EUR/kWh, index in EUR/MWh). Any index name other than TTFDAW-RLP-M
    fails the parse.
- VAT: "Alle prijzen en tarieven zijn inclusief 6% btw" into `card_vat_rate`.
- DSO table, after "Netbeheerder": columns T1 vaste term (€/jaar), T1
  proportionele term (€cent/kWh), T2 vaste term, T2 proportionele term,
  Meteropname (€/jaar). Meteropname is the Flemish data management fee
  (18,92) and a dash on the Walloon rows. No T3: the card only applies up to
  100 MWh a year.
  - Flemish labels as printed: FLUVIUS ANTWERPEN, HALLE VILVOORDE, IMEWO,
    KEMPEN (IVEKA), LIMBURG, MIDDEN VLAANDEREN (INTERGEM), WEST, ZENNE DIJLE.
    The bracketed old names are labels only; the figures are the new areas'
    (they match Fluvius's 2026 tariffs and Engie's card).
  - Walloon labels: the five ORES sub-areas (must agree, collapse to `ores`)
    and TECTEO RESA (TECTEO - RESA on the older cards).
- Transport: not a column but a note, "Voor het jaar 2026 raamt Fluxys deze
  kosten op 0,165 €cent/kWh (incl. btw)", passed as `transport=`.
- Excise, "Federale Accijns (€cent/kWh)" with columns "Bijzondere accijns" and
  "Energiebijdrage": "Verbruik tussen 0 en 12.000 kWh 1,0929 0,0000" and
  "Verbruik > 12.000 kWh 1,1830 0,0000". Read as printed; the two
  contribution cells must agree.
- Walloon connection fee: "Aansluitingsvergoeding < 1 GWh 0,0075 €cent/kWh",
  mandatory for Wallonia. The row above it ("0 - 100 kWh/jaar 0,0075 €") is
  the same rate on the first 100 kWh and is not read.

## Index

- TTFDAW-RLP-M: the RLP-weighted mean of the ICIS Heren TTF day-ahead and
  weekend mid prices over the delivery month (eneco.be/indexatie), known only
  at month end, so `settled` is False. The card's Maandprijs is the formula at
  the last closed month, footnoted "(08/2026: €61,6374/MWh)".
- `fetch_index` reads
  https://cdn.eneco.be/downloads/nl/b2c/acq/indexatieparameters-aardgas.pdf:
  columns TTF-DAHW, TTFDAW-RLP, TTFDAW-RLP-M, ZTP-S41, ZTP-S31, TTF103,
  TTF303, one row per month from January 2023. Every column is returned, the
  quarterly ones month by month as printed.
- The PDF leaves cells empty (TTFDAW-RLP-M before March 2023, quarterly
  columns until the quarter closes), and its text layer cannot say which
  column a figure is in. `index_table_text` places each figure under the
  heading it sits beneath (pdfplumber word positions) and writes a dash in
  the empty cells; `parse_index_text` reads that text.
- The PDF prints two decimals where the footnote prints four. Over the 22
  Flex and Flex One cards checked, April 2023 to September 2026, the
  footnoted value gives the printed Maandprijs to the hundredth on all but
  December 2024 (5,8452, printed 5,84), and the PDF's value on all but
  January 2026 (4,0153, printed 4,01). The gap is under 0,0001 EUR/kWh
  either way.

## Archive

- `fetch_for_month` builds the `01YYMM` URL. Flex issues are on the CDN back
  to at least January 2020, Vast from January 2020 with a gap from January
  2022 to April 2023, Flex One only from September 2026 (earlier months 404,
  which is a month with no card).
- No `02YYMM` reissue was found for gas or electricity Flex or Vast from 2019
  to 2026, so only the first issue is asked for.
- Cards before 2025 list Gaselwest, Iveka, Iverlek, Intergem and Sibelgas and
  fail `require_region` for Flanders. Their Walloon rows read from February
  2024; the April, May and December 2023 and January 2024 cards checked print
  five different ORES rows and are refused.
- A month ahead of today (Home Assistant's clock) is not asked for; a card
  naming another month is refused.

## Known quirks and card errors

- pdfplumber sets the tax block on the DSO rows' lines and reads the wrapped
  Midden-Vlaanderen label as "F (I L N U T V E I R U G S E M M I ) DDEN
  VLAANDEREN", so the card is read with pypdf.
- pypdf wraps that label before "(INTERGEM)" and leaves the cells on the
  second line; the module joins the two lines before reading the table.
- The excise labels are set with no-break spaces.
- The September 2026 Flex One card's footer names
  "BC_032_012608_NL_ENECO_GAS_FLEX_ONE" although its version line and file
  are 012609. The month comes from the title sentence, not the footer.
- DSO proportional terms are rounded to two decimals (Fluvius publishes five:
  Antwerpen 2,25641 printed 2,26), the excise to four (1,09286 printed
  1,0929), transport to three (0,16536 printed 0,165).
- Indexation PDF: TTF-DAHW prints 30,04 for October 2025 but 45,65 for
  November and December 2025, and TTFDAW-RLP 45,95 for all of Q4 2025; 45,65
  and 45,95 are also its Q2 2026 values. No residential gas product uses
  these two columns.

## Tests

- Flex energy leg, formula and footnote check, both regions:
  `test_flex_is_priced_at_the_last_known_index`.
- Flex One base: `test_flex_one_has_its_own_base`. Vast:
  `test_vast_is_fixed`.
- Product title check: `test_card_of_another_product_is_refused`.
- Flemish DSO rows incl. the wrapped label, transport note:
  `test_flanders_rows_include_the_wrapped_label`.
- Walloon DSO rows and levies: `test_wallonia_table_collapses_the_ores_sub_areas`,
  `test_wallonia_levies`; stale levies as printed:
  `test_pre_august_levies_are_read_as_printed`.
- Fail loud: connection fee, transport note, formula, Brussels, unknown
  contract.
- Pre-2025 layout: `test_pre_2025_card_reads_for_wallonia_only`.
- Discovery: `test_listing_links_each_product_once`,
  `test_fetch_reads_the_card_the_listing_links`.
- Archive: the `test_fetch_for_month_*` tests (URL, Brussels midnight,
  wrong month, transient failure, 404 before a product existed, future
  month and Brussels region).
- Index: `test_index_pdf_places_each_figure_under_its_heading`,
  `test_published_index_reproduces_the_cards`,
  `test_index_text_without_the_monthly_column_fails_loud`,
  `test_fetch_index_reads_the_indexation_pdf`.
