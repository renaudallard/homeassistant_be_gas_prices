# Mega

Module: `custom_components/be_gas_prices/providers/mega.py`. Tests:
`tests/test_mega.py`, fixtures in `tests/fixtures/mega/`.

## Where the cards are

Every card is a PDF on Mega's CDN:

    https://my.mega.be/resources/tarif/Mega-FR-NG-B2C-<BX|VL|WL>-<MMYYYY>-<Suffix>.pdf

`NG` is gas (`EL` is electricity), `B2C` residential, `MMYYYY` the card's
month. The suffix names the product and, on most products, the day and month
the issue took effect (`Smart0109` is Smart Flex issued on 1 September). It
cannot be predicted, and it is not the electricity suffix (Online Flex is
`Online0109` for gas, `Online0109-Green` for electricity), so `fetch` reads
the listing, https://www.mega.be/fr/energie/cartes-tarifaires, and takes the
`href` of the anchor carrying `data-product-element="<Product>"` whose URL is
in the `Mega-FR-NG-B2C-<region>-` segment. When the listing drops one
region's anchor of a product while the card is still published (the
electricity listing lost Dynamic Wallonia overnight in July 2026), another
region's URL is rewritten to the region's segment; a card not published
there fails on the CDN's stub, one for another region on `require_region`.

The CDN answers a card it does not have with a 200 HTML page (11721 bytes in
September 2026). `fetch_pdf_text` refuses it by its magic bytes.

Mega's CDN sends every card with `Content-Encoding: UTF-8`. curl asked for
compressed transfer fails on it (error 61, "Unrecognized content encoding
type"); aiohttp only decodes gzip, deflate, br and zstd, so it reads the body
as is. Checked live on 2026-09-29.

## Products and regions

| Contract id | Label | Kind | Listing name | Card prints | Suffix (09/2026) | Regions |
|---|---|---|---|---|---|---|
| mega_smart_flex | Mega Smart Flex | indexed (ZTP) | Smart Flex | Smart Flex 2Y | Smart0109 | V W B |
| mega_cosy_flex | Mega Cosy Flex | indexed (ZTP) | Cosy Flex | Cosy Flex 2Y | Cosy0809 | V W B |
| mega_online_flex | Mega Online Flex | indexed (ZTP) | Online Flex | Online Flex 2Y | Online0109 | V W B |
| mega_prepaid_flex | Mega Prepaid Flex | indexed (ZTP) | Prepaid Flex | Prepaid Flex | Prepaid | V W B |
| mega_offpeak_flex | Mega Off-peak Flex | indexed (TTF) | Off-peak Flex | Offpeak Flex | Offpeak-Bi-Var | V W B |
| mega_offpeak_impact | Mega Off-peak Impact | indexed (TTF) | Off-peak Impact | Offpeak Impact Flex | Offpeak-Impact-Var | W |
| mega_smart_fixed | Mega Smart Fixed | fixed | Smart Fixed | Smart Fixed 2Y | Smart0809-Fixed | V W B |
| mega_cosy_fixed | Mega Cosy Fixed | fixed | Cosy Fixed | Cosy Fixed 2Y | Cosy0109-Fixed | V W B |
| mega_online_fixed | Mega Online Fixed | fixed | Online Fixed | Online Fixed | Online0109-Fixed | V W B |
| mega_prepaid_fixed | Mega Prepaid Fixed | fixed | Prepaid Fixed | Prepaid Fixed | Prepaid0109-Fix | V W B |
| mega_offpeak_fixed | Mega Off-peak Fixed | fixed | Off-peak Fixed | Offpeak Fixed | Offpeak-Bi0109-Fix | V W B |
| mega_zen_fixed | Mega Zen Fixed | fixed | Zen Fixed | Zen Fixed 3Y | Zen0109-Fixed | V W B |

There is no Dynamic gas card. The listing also links `SME Fixed` and `SME
Flex` gas cards in the `B2B` segment; they are professional and not
registered. The electricity integration leaves both Prepaid products out as
top-up products; the gas Prepaid cards bill metered consumption on the same
formula and table as the others ("payez votre energie par avance de 3
mois"), so they are registered here.

## How each figure is read

The card is read with pypdf (`fetch_pdf_text`), which puts one value per line.
pdfplumber lays the table out better but took 12,9 s on a Smart Flex card on
a Raspberry Pi against 1,5 s for pypdf.

- Product: the line between `Gaz naturel` and `(Variable)` or `(Fixe)`, less
  a trailing term (`2Y`, `3Y`), must equal the contract's card name. The
  January 2025 card printed `Smart Flex` without a term.
- Month: `Prix du mois MM/YYYY`. `valid_until` is the end of that month.
- VAT: `TVA 6% incluse`, stored as `card_vat_rate`. Mandatory.
- Price and fee: the figure after `Coût énergie (c€/kWh)` and after
  `Redevance fixe (€/an)`.
- Formula (Flex only): `La formule tarifaire pour le gaz est la suivante
  (HTVA): ZTP x 1,08 + 1,15 c€/kWh`. The index is in c EUR/kWh, so factor =
  1,08 / 1000 x 1,06 per EUR/MWh and base = 1,15 / 100 x 1,06 EUR/kWh. The
  index must also be defined on the card as `moyenne arithmétique des
  cotations journalières Day Ahead et Weekend ZTP (EGSI) durant le mois de
  fourniture` (or TTF), which is the definition of the series the index
  page publishes.
- `IndexedRates.price` on a Flex card is the last known price, not the
  headline: `Les derniers prix constatés ... pour le mois de août 2026 (...)
  sont les suivants (c€/kWh) : Compteur mono-horaire : 8.28`. The headline
  (8.34) is `une prévision des prix de l'énergie pour une livraison les 12
  prochains mois`. The 8.28 is the card's own formula at the previous
  month's index. The headline is used only if the sentence is missing.
- DSO table: between `Prix de la distribution et du transport` and `Taxes,
  redevances`. Columns: the three proportional terms (0-5000, 5001-150 000,
  150 001-1 000 000 kWh, c EUR/kWh), the three fixed terms (EUR/year), then
  `Gestion des données (€/an)` on the Flemish card and `Mesure et comptage
  (€/an)` on the Brussels one; the Walloon card has no metering column.
  `_table_rows` joins each label and its figures into one line and completes
  labels wrapped at a hyphen or an open parenthesis (`Fluvius Halle-` /
  `Vilvoorde`, `ORES (Brabant` / `wallon)`) before `read_dsos`.
- Transport: `Coût du transport` then `0.165`, printed once, passed as
  `transport=`.
- Excise: `Consommation entre 0 et 12.000 kWh` and `Consommation de plus de
  12.000 kWh`, one figure each. Cards before August 2026 print the energy
  contribution in a second column (`0.10577` on both slices); later ones
  state `La contribution énergétique est fixée à 0 à partir du 1er août
  2026`. One of the two is required.
- Walloon connection fee: `Redevance de raccordement` then `0.0075` (c
  EUR/kWh). Mandatory on the Walloon card.
- Brussels levy: after `Obligations de Service Public`, each amount is the
  line after `/heure`, `/heure*` or `/heure**` (the caliber prints as `6 ou 10
  m`, `3`, `/heure*`). Nine amounts, in order, go to `osp_table`.

Decimals use a dot and drop trailing zeros (`2.5`, `376`, `15.9`); the formula
uses a comma. `to_float` reads both.

## Index values

`fetch_index` reads https://www.mega.be/fr/energie/indexation-de-nos-produits-variables
(the Dutch page carries the same figures). Each series is a table of rows
`<tr data-line="<name>" data-to="YYYY-MM-01">` with three cells: from, to,
`€0.0617121` in EUR/kWh excluding VAT. The two gas series the cards use:

- `ZTP mensuel`: "moyenne arithmétique des cotations journalières Day Ahead
  et Weekend ZTP (EGSI) durant le mois de fourniture", monthly from January
  2023.
- `TTF1`: "moyenne arithmétique mensuelle des cotations journalières Day
  Ahead et Weekend TTF (EGSI) durant le mois de fourniture", monthly from
  January 2021.

A row counts when it runs from the 1st of a month to the 1st of the next; the
value is stored x 1000 as EUR/MWh under `YYYY-MM` of its first day. On
2026-09-29 the last row of both was August 2026 (ZTP 61,7121, TTF 61,54081
EUR/MWh). The page also lists older gas series no card uses (TTF101, TTF103,
ZTP S41) and the electricity ones; they are ignored. Its "Ces données n'étant
pas actualisées" line is an element hidden by default (`class="hidden"`),
not a statement about the data.

Every Flex card checked (July, August and September 2026, all six products)
prints a last known price the formula reproduces at this page's value to
0,01 c EUR/kWh. The October 2026 cards do not all: their September price on
ZTP is 0,02 to 0,03 c EUR/kWh under the formula at the page's ZTP mensuel of
75,49847 EUR/MWh (Smart and Cosy Flex 9,84 for 9,862, Online Flex 9,52 for
9,544, Prepaid Flex 8,83 for 8,855), as if the index were about 75,26 to
75,32, while Off-peak Flex and Off-peak Impact, on TTF, still round the same
(9,17 for 9,173). The earlier cards were also published on the last day of
the month they price, so the publication date does not explain the gap.
Billing is not affected: a month the page lists is priced at the page's
value. The card alone would not do: its rounded price pins the index only to
about 0,04 EUR/MWh.

## Archive

`fetch_for_month` reads the listing, then rewrites the current URL: the
`-MMYYYY-` segment and the month of the suffix's issue date, whose day
becomes `01` (`Cosy0809` for September becomes `Cosy0108` for August). A
suffix without a date (`Prepaid`, `Offpeak-Bi-Var`) only changes its
`MMYYYY`. The listing's month keeps the listing URL unchanged.

Why the 1st: Mega reissues some cards mid-month. Cosy Flex and Smart Fixed
were issued as `0109` (31 and 27 August) and again as `0809` (7 September),
with the same prices and a larger ristourne, and the listing links the
reissue. `Cosy0808` and `Smart0808-Fixed` for August are HTML stubs while
`Cosy0108` and `Smart0108-Fixed` are the cards. Every month checked has an
issue dated the 1st: the electricity archive from January to September 2026
(reissues dated the 8th in January, April and September), every Walloon
gas card fetched for August 2026, and gas Smart Flex in January 2024, January and
September 2025, January and June 2026.

A product Mega stops listing has no archive through this path. Off-peak Flex
has no January 2026 card at `Offpeak-Bi-Var` (stub). Older cards parse:
January 2025, January, March and June 2026 were checked.

## Quirks and card errors

- The rounding of exact half cents goes down on Mega's cards: Fluvius Kempen
  T1 fixed term 16.16 (Fluvius 15,25 excl. VAT x 1,06 = 16,165; Engie prints
  16,17), Zenne-Dijle T3 fixed term 660.64 (623,25 x 1,06 = 660,645; Engie
  660,65), Fluvius West T2 proportional 1.05 (1,05500). Read as printed.
- Proportional terms are printed with two decimals (ORES 4.29 against the
  CWaPE 4,28944), transport as 0.165 (Fluxys 0,16536).
- The Prepaid Fixed card prints `Prepaid-Fix` where the other cards print
  `Carte tarifaire`; nothing is anchored on either.
- The Smart Flex card links `...-Cosy0109.pdf` as the Cosy tariff while the
  listing links `Cosy0809`; both are September Cosy Flex cards.
- The last known price is the card's current formula at last month's index:
  the September Smart Flex card prices August at `+ 1,15` while the August
  card's formula was `+ 1`.
- First-year ristourne: most cards grant one, a c EUR/kWh reduction, a
  reduction of the fixed fee (alone on the Off-peak cards, 32 EUR in
  October 2026), or both. The October 2026 cards grant it "dès la première
  facture d'acompte" during the first contract year, with no cap or
  direct-debit condition; most prorate the fee reduction by days of
  supply, the Walloon and Brussels Zen Fixed cards do not. Up to
  September 2026 it was paid on the first regularisation after twelve
  uninterrupted months (fourteen on Zen Fixed), capped at 848 EUR, some
  cards only with direct debit or with a direct-debit supplement. Prepaid
  grants none. It is not part of the tariff and is not read.

## Tests

- `test_smart_flex_is_priced_at_the_last_known_ztp`: WL Smart Flex energy leg,
  formula coefficients, month, VAT.
- `test_flex_formula_reproduces_the_printed_price`: the six Flex cards, price,
  fee, formula, index name, and the formula at the index page's August value.
- `test_the_forecast_is_kept_when_the_last_known_price_is_missing`.
- `test_fixed_cards`: the six fixed cards, price and fee.
- `test_wallonia_table_collapses_the_ores_sub_areas`, `test_wallonia_levies`,
  `test_flanders_table_carries_the_data_management_fee`,
  `test_brussels_card_reads_the_per_meter_levy`: full rows per region,
  transport, metering, connection fee, excise, levy table.
- `test_july_card_prints_the_old_excise_and_the_energy_contribution`.
- Refusals: another product, another region, unknown contract, and a card
  missing the formula, the index definition, the contribution statement,
  the connection fee, transport or the month.
- Listing: `test_listing_links_the_gas_card`,
  `test_every_contract_is_on_the_listing_where_it_is_sold`.
- Archive: `test_archive_url_moves_both_months` and the `fetch_for_month`
  tests (URL built, Brussels clock, month refusal, CDN stub, transient
  failures, future month).
- Index: `test_index_page_reads_both_series_by_delivery_month`,
  `test_index_page_without_the_series_fails_loud`,
  `test_fetch_index_reads_mega_s_page`.
