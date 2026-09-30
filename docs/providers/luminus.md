# Luminus

Module: `custom_components/be_gas_prices/providers/luminus.py`. Tests:
`tests/test_luminus.py`, fixtures in `tests/fixtures/luminus/`.

## Where the cards are

- Current card:
  `https://www.luminus.be/api-next/get-pricelist/?documentSlug=<slug>&energyType=gas&language=fr&tabValue=<Wallonia|Flanders>`,
  the electricity endpoint with `energyType=gas`. The response is named like
  `LUMINUS_PL_202609_ZGR2A0_FR_WAL_ComfyFlex_Gas_1_year_Online_Sales_Luminusbe.pdf`.
- `tabValue=Brussels` answers 404; an unknown slug answers 500.
- The endpoint does not refuse an electricity-only slug: `smartflex` serves
  the ComfyFlex gas card and `dynamic` the MaxxFlex one. The file name does
  not tell Comfy+ from Comfy either (`ZGR2B0I_..._Comfy_Gas`). The parser
  compares the product in the card title with the contract.
- The body starts with a UTF-8 BOM before `%PDF`; the shared fetcher strips it.
- Probe: `Content-MD5` of a HEAD on the card URL. It is the MD5 of the body
  (checked against a download). The blob is rewritten without the card
  changing: on 29 September 2026 all sixteen cards had a Last-Modified of
  01:00 that night while the PDFs carry a CreationDate of 17 September, so
  Last-Modified and ETag are not used.

## Products and regions

| Contract id | Label | Slug | Card code | Kind | Index |
|---|---|---|---|---|---|
| `luminus_comfy` | Luminus Comfy | comfy | ZGR2B0 | fixed | |
| `luminus_comfy_plus` | Luminus Comfy+ | comfy-plus | ZGR2B0I | fixed | |
| `luminus_comfyflex` | Luminus ComfyFlex | comfyflex | ZGR2A0 | indexed, quarterly | TTF DAHW |
| `luminus_comfyflex_plus` | Luminus ComfyFlex+ | comfyflex-plus | ZGR2A0I | indexed, quarterly | TTF DAHW |
| `luminus_maxxfix` | Luminus MaxxFix | maxxfix | ZGR2B2X | fixed | |
| `luminus_maxxflex` | Luminus MaxxFlex | maxxflex | ZGR2A2X | indexed, monthly | TTF DAH M |
| `luminus_basicfix` | Luminus BasicFix Online | basicfix | ZGR1B2D | fixed | |
| `luminus_basicflex` | Luminus BasicFlex Online | basicflex | ZGR1A0D | indexed, monthly | TTF DAH RLP M |

- Every product is in Flanders and Wallonia (all sixteen cards fetched on
  29 September 2026). No Brussels card. The social tariff ("Tarif social
  Gaz" in the archive list) is CREG-set and not listed.
- The energy leg differs by region (September 2026, Wallonia against
  Flanders: Comfy 9,60/9,37, ComfyFlex base 2,0204/1,8004), so the configured
  region's card is read.
- Comfy and Comfy+ are fixed for the first 12 months of delivery ("Luminus
  s'engage à ne pas augmenter les prix ... pendant les 12 premiers mois"),
  open-ended after. MaxxFix and BasicFix are two-year fixed contracts.

## How each figure is read (pypdf text)

- Title, first line of the card body: "Luminus ComfyFlex Gaz(septembre 2026)".
  It gives the product (checked with brand, "Gaz" and "Online" stripped, since
  the 2025 cards title "BasicFix Gaz" what the 2026 ones title "BasicFix
  Online Gaz") and the month for `publication_label` and `valid_until`.
- VAT: "La TVA sur les prix indiqués et les éventuelles promotions est de 6 %"
  into `card_vat_rate`, and the multiplier for the formulas. Mandatory.
- Energy: "Énergie (c€/kWh) 7,00" under "Prix du jour", and "Redevance fixe
  (€/an) 50,00". The "Estimation annuelle" row below is a 12-month forecast
  and is not read.
- Formula, on the whitespace-collapsed text after "Index (HTVA)":
  - ComfyFlex: "0,1004 x TTF DAHW + 0,0000 x TTF 1-0-3 + 2,0204". The TTF
    1-0-3 term must be weighted zero or the parse fails, since one index
    cannot resolve it.
  - MaxxFlex: "0,1001 x TTF DAH M + 1,5650"; BasicFlex: "0,1001 x TTF DAH
    RLP M + 0,5609".
  - c€/kWh excluding VAT for an index in EUR/MWh: factor = A / 100 x 1,06,
    base = C / 100 x 1,06. An index name other than the three above fails.
- DSO table, after "Coûts de distribution": per tier "Variable (c€/kWh)"
  then "Fixe (€/an)" for T1 (<= 5.000 kWh), T2 (<= 150.000) and T3
  (> 150.000), then "Coûts de transport (c€/kWh)".
  - Wallonia: ORES (Brabant Wallon), ORES (Hainaut Gaz), ORES (Luxembourg),
    ORES (Mouscron), ORES (Namur) (must agree) and TECTEO RESA.
  - Flanders: the eight Fluvius areas as `FLUVIUS_LABELS` spells them.
  - A stray region tag ("WAL", "FL") sits alone on the line above the rows.
- Taxes, "3 Taxes et redevances : WAL" (or FL): the row labels, then their
  figures one a line in the same order, up to "INFORMATION SUR VOTRE TARIF"
  or, on the February 2026 indexed cards, which leave that heading out, the
  VAT note "La TVA sur les prix indiqués".
  - "Cotisation sur l"énergie": a dash, read as 0.
  - Excise: the row prints the low band only; both come from its footnote
    "0-12.000 kWh : 1,0929 c€/kWh, >= 12.001 kWh : 1,1830 c€/kWh".
  - "Redevance de raccordement": 0,0075 c€/kWh in Wallonia (mandatory; a
    dash there fails the parse), a dash in Flanders.
  - The heading names the regions the card covers; a card whose heading does
    not name the configured region is refused.

## Card layout until December 2025

- One card for both regions (the archive serves identical bytes for either
  `region`). The parser reads it for either region.
- The DSO table has a last column "Tarif gestion des données / Activité de
  mesure et de comptage (€/an)": 18,56 on the Fluvius rows in 2025, a dash
  on the Walloon rows. Its heading switches the column list.
- The tax block heading is "Taxes et redevances : FL WAL" and prints three
  figures for Flanders then three for Wallonia.
- Readable back to January 2024 for Wallonia and January 2025 for Flanders.
  The 2024 cards name Fluvius areas that merged away and fail
  `require_region` for Flanders; the 2023 cards print five different ORES
  rows or other levies; the 2021 quarterly cards are titled by quarter
  ("juillet-septembre 2021"). Each of these is a month with no card.

## Index

- TTF DAHW: mean of the ICIS TTF day-ahead (working days) and weekend
  assessments over the delivery quarter, known at quarter end. `period`
  "quarter"; the card prints its price at the last closed quarter ("valeur de
  l'indice du 2ième trimestre 2026" on the September card).
- TTF DAH M: the same over the delivery month. TTF DAH RLP M: the same month
  weighted by the RLP load profiles Synergrid publishes. The cards price at
  the previous month ("valeur de l'indice de août 2026").
- None is settled.
- `fetch_index` reads the indexation page
  https://www.luminus.be/fr/particuliers/energie/parametres-d-indexation/, takes
  the `fileId` of its "Télécharger les indices" link (one distinct id, or the
  fetch fails) and downloads
  `https://www.luminus.be/api-next/download/?fileId=<id>&openFile=true`. The id
  changes with each upload and old ids answer 500 (checked against the
  December 2025 and March 2026 pages in the Wayback Machine).
- The PDF sets ten tables in two rows on its first page, three years each (2024 to 2026 in
  September 2026), with titles TTFDAHW, TTFDAHM and TTFDAH RLP M for the gas
  indices the cards name TTF DAHW, TTF DAH M and TTF DAH RLP M. Cells of
  periods not yet known are empty. `index_table_text` finds each title by the
  year headings just below it (the formula beside it names the index again),
  places every figure under its year by pdfplumber word position and writes a
  dash in empty cells; `parse_index_text` reads that text and fills each month
  of a quarter.
- Agreement with the cards: the index PDF reproduces every value the cards
  name that falls in its window: TTF DAHW Q4 2024 42,831 (card 42,83), Q1
  2025 46,853, Q2 2025 35,358, Q3 2025 32,436, Q4 2025 30,042, Q1 2026 39,680,
  Q2 2026 45,649 (card 45,65); TTF DAH M May 2025 34,737, July 2026 53,206,
  August 2026 61,438; TTF DAH RLP M May 2025 34,521, July 2026 52,991,
  August 2026 61,637.

## Archive

- `https://www.luminus.be/api/pricelist/products?language=FR&customerSegment=Residential&energyType=Gas&region=<Wallonia|Flanders>&signing=YYYY-MM`
  lists the month's products with an id ("Luminus ComfyFlex Gaz",
  `a1p0800000BygAeAAJ`), matched on the same product key as the title.
- `https://www.luminus.be/api/pricelist/pdf?language=FR&productId=<id>&date=YYYY-MM&region=<...>&inline=true`
  serves the card, named `..._Direct_Mail_Archive_Price_Lists.pdf`. It has the
  same figures as the online card of the month but not the new-customer
  campaign.
- The product list also answers for a future month (with new ids for Comfy
  and Comfy+ in October 2026), and the PDF endpoint then returns
  `"Pdf not available."` as JSON under a 200. A month ahead of today (Home
  Assistant's clock) is not asked for.
- A card naming another month than the one asked for is refused; transient
  failures raise.

## Campaigns (not modelled)

September 2026 online cards, both regions:

- Comfy: "remise de 50% sur les coûts énergétiques sur votre consommation
  annuel pendant 12 mois pour la conclusion d'un contrat Luminus Comfy Gaz en
  septembre 2026", "répartie au pro rata sur vos prochains décomptes".
- ComfyFlex: the same at 44%.
- Comfy, Comfy+, ComfyFlex, ComfyFlex+: boiler maintenance through a separate
  Luminus Entretien Chaudière contract at 5,45 EUR a month instead of 7,95 for
  24 months (60 EUR in all).
- Every card but BasicFix and BasicFlex: a one-time 20 EUR for paying by
  direct debit.
- No free-kWh campaign on any gas card.
- The archive's Direct Mail cards carry none of the new-customer campaigns.

## Known quirks and card errors

- The 2026 Flemish cards print no data management fee. Fluvius's 2026 gas
  databeheer is 18,92 EUR/year and Engie prints it; Luminus printed 18,56 in
  2025. `metering_per_year` is 0 on these cards.
- The cards name the monthly index "TTF DAH M" in the formula and "TTF DAHW
  M" in the paragraph below it (BasicFlex: "TTF DAH RLP M" and "TTF DAHW RLP
  M"); the index PDF titles them TTFDAHM and TTFDAH RLP M.
- The formula coefficients are rounded ("Les montants mentionnés sont
  arrondis"): at the named index the formula lands up to 0,006 c€/kWh off
  the printed price (Flemish MaxxFlex 7,9446 against 7,95, Flemish
  ComfyFlex+ 7,1642 against 7,17).
- ComfyFlex says the settlement uses quarterly means where known and monthly
  means otherwise; the index PDF publishes TTFDAHW by quarter only.
- DSO figures are rounded to two decimals (Fluvius Antwerpen 2,25641 printed
  2,26), the excise to four (1,09286 printed 1,0929), transport to four
  (0,16536 printed 0,1654).
- The Walloon connection fee footnote: "Non applicable sur les premiers
  100 kWh. Cette redevance est majorée d'un montant forfaitaire de €
  0,0075", which is the same 0,0075 EUR the per-kWh rate gives on 100 kWh.
- pypdf prints the footnotes of page 1 before the title; the title regex is
  anchored on a line start and the "(" after "Gaz" so they cannot match.

## Tests

- Indexed legs and the formula check at the named index:
  `test_comfyflex_indexes_quarterly_on_ttf_dahw`,
  `test_flanders_card_prices_the_energy_differently`,
  `test_comfyflex_plus_is_its_own_card`,
  `test_maxxflex_indexes_on_the_delivery_month` (both regions),
  `test_basicflex_indexes_on_the_rlp_weighted_month`.
- Fixed legs: `test_fixed_cards` (Comfy, Comfy+, MaxxFix, BasicFix).
- Product and region checks: `test_card_for_another_product_is_refused`,
  `test_card_for_another_region_is_refused`, `test_unknown_contract_is_refused`.
- DSO rows and levies: `test_wallonia_table_collapses_the_ores_sub_areas`,
  `test_wallonia_levies`,
  `test_flanders_table_has_every_fluvius_area_and_no_data_management`.
- 2025 layout: `test_2025_card_serves_both_regions`,
  `test_2025_title_without_the_online_marker_is_the_same_product`.
- February 2026 indexed card without its tarif heading:
  `test_february_2026_card_without_the_tarif_heading_is_read`.
- Fail loud: `test_forward_term_weighted_in_fails_loud`,
  `test_walloon_card_without_connection_fee_fails_loud`.
- Index: `test_index_pdf_places_each_figure_under_its_year`,
  `test_index_table_holds_the_values_the_cards_name`,
  `test_card_index_names_are_the_published_ones`,
  `test_index_rows_of_the_wrong_period_are_refused`,
  `test_indexation_page_links_the_index_pdf`,
  `test_fetch_index_follows_the_page_link`.
- Discovery and probe: `test_fetch_builds_the_regional_url`,
  `test_probe_reads_the_content_md5`.
- Archive: the `test_fetch_for_month_*` tests (product id and URLs, wrong
  month, transient failure, "Pdf not available", future month, Brussels
  midnight).
