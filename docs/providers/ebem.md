# EBEM

Module: `custom_components/be_gas_prices/providers/ebem.py`. Tests:
`tests/test_ebem.py`, fixtures in `tests/fixtures/ebem/`.

## Where the cards are

- Listing: https://www.ebem.be/tarieven/ (a 302 to `/tarieven/tarieven/`),
  "Download Ebem Tariefkaarten Gas". Each card sits under an opaque media
  folder that changes per file:
  `/media/<hash>/ebem_tariefkaart-gas-MM-YYYY.pdf`.
- `fetch` takes the newest month the listing links. One PDF holds both
  products, so a sweep reading both parses it once through the text memo.
- No probe: the listing answers with `Cache-Control: private` and neither
  Last-Modified nor ETag.

## Products and regions

| Contract id | Label (card heading) | Kind | Regions |
|---|---|---|---|
| `ebem_variable` | EBEM Aardgas Variabel | indexed, monthly | flanders |
| `ebem_gas_plus` | EBEM G@S+ | indexed, monthly | flanders |

- "Variabel tarief voor levering aan particulieren en klein zakelijk
  verbruik", "DIT CONTRACT IS VAN ONBEPAALDE DUUR"; G@S+ is "Webonly". The
  DSO table is Fluvius only.
- Each product is the block from its "TARIEFKAART EBEM <product>" heading to
  the next heading or to "Nettarieven aardgas". A card without the block
  asked for is refused, and neither block can lend the other its formula.

## How each figure is read (pdfplumber layout text)

- Card month: the block heading, "TARIEFKAART EBEM Aardgas
  Variabel_26_04 september 2026" (the 2025 cards print "... - januari
  2025").
- Energy row: "0,105 ZTP +0,675 7,1661 c€/kWh 7,5961 c€/kWh 7,2070 c€/kWh
  7,6394 c€/kWh". The formula in c EUR/kWh excl. VAT with ZTP in EUR/MWh,
  then the price excl. and incl. VAT at the previous month's index, then both
  at the VNR estimate. `price` is the second figure, 7,5961 (a known index,
  not a forecast). factor = 0,105 / 100 x 1,06, base = 0,675 / 100 x 1,06,
  grossed by the rate the header prints ("INCL. BTW 6%").
- Fee: "Vaste vergoeding 66,04 €/jaar 70,00 €/jaar", the second (incl. VAT).
- DSO table, "NETBEHEERDER" to "Belastingen, heffingen": T1 VAST (EUR/year),
  VARIABEL (c EUR/kWh), T2 VAST, VARIABEL, T3 VAST, VARIABEL, TARIEF
  DATABEHEER (EUR/year), TRANSPORT (EUR/MWh, `TRANSPORT_MWH`). Labels match
  the shared `FLUVIUS_LABELS` ("Fluvius Halle Vilvoorde" scores 0,957).
- Excise: "0-12 MWh 1,09286 c€/kWh" and "12-20.000 MWh 1,18296 c€/kWh", each
  on its own line between the lines of a paragraph.
- Energy contribution: "Huishoudelijk: 0,00000 c€/kWh" (0,105764 before
  August 2026), mandatory. "Zakelijk (excl BTW): 0,09978" is not read.
- No Flemish levies on the card.

## Index

- The formula cell prints "ZTP" over "RLP0"; the parameters document calls
  it "De gewogen parameter ZTP-RLP0", weighted with "het gemiddelde van de
  RLP's in Vlaanderen van Synergrid". The index is filed as `ZTP-RLP0`.
- The card says "Voor de huidige maand is deze index nog niet gekend.
  Vorige maand bedroeg deze index 61,82", so the delivery month's value is
  only known later: `settled` is False.
- `fetch_index` reads the newest `ebem_parameters_indexen-MM-YYYY.pdf` the
  listing links ("Download uitleg en waarden parameters en indexen"), page 4,
  column "Argus ZTP-RPL". Page 4 prints it to the right of TTF101, one line
  per month for both ("juni 93,05595 31,32900 juni 34,08 35,66 44,70"); the
  values follow the month's second occurrence, one per year heading, dots
  for a month not over. pypdf scatters this table, so it is read with the
  layout text too.
- Reliable: for every card from January 2025 to September 2026 its
  "vorige maand" figure equals the document's previous month (December 2024
  44,81 to August 2026 61,82).

## Archive

- The same listing keeps every month back to January 2023 under its month.
  `fetch_for_month` takes the link of the month asked for; no clock is
  involved.
- Names before 2025 break the pattern (`gas-10-2024_web2.pdf`,
  `gas-09-20244_web.pdf`, `gas-mei-2023_web.pdf`) and those cards list
  Gaselwest, Iveka, Iverlek, Intergem and Sibelgas, so they are not linked
  as cards at all (None without a download).
- Every 2025 and 2026 card parses for both products. A card naming another
  month than its file is refused.

## Known quirks and card errors

- Fluvius Kempen T1 proportional prints 2,12 on every card from January
  2025 to September 2026. It is the 2025 figure: Frank's 2025 cards print
  Kempen T1 as 14,70 and 2,12 like EBEM's, and from January 2026 16,17 and
  2,27, where EBEM moved the fixed term to 16,17 and kept 2,12. Fluvius's
  2026 tariff is 2,27489 and every other supplier prints 2,27 or 2,275.
  Read as printed.
- Imewo T1 proportional 2,62 and West T2 proportional 1,06 (Fluvius 2,62774
  and 1,05500): rounding, where energie.be prints 2,63 and 1,05.
- Tolerated: "17.59" with a dot (Halle-Vilvoorde T1 fixed) and a stray
  line "7" inside the table (both on every 2026 card), "75 ,60" (Antwerpen
  T2 fixed, every 2025 card, joined before the table is read), "7,5431
  €/kWh" for c EUR/kWh (G@S+, August and September 2026), a missing space
  before the unit ("4,3154c€/kWh"), and a header printing "€/kWh" over
  proportional terms in c EUR/kWh (January 2025 to April 2026).
- September 2025 Aardgas Variabel prints 4,2817 incl. VAT for 4,0403 excl.,
  which is 4,2827; read as printed. Every other card matches its formula at
  the named index to the fourth decimal.
- March 2025 says "Deze transportkost van 1,44 €/MWh (excl BTW), 1,53 €/MWh
  (incl BTW)" above a table printing 1,62. The table is read.
- The parameters page heads the column "Argus ZTP-RPL" but says the value is
  computed "aan de hand van de officiële slotkoersen ... van de EEX-beurs";
  which assessment it is was not verified.
- The September 2026 card limits itself to "een maximum jaarverbruik van
  100.00 kWh" while printing a T3 up to 1 000 000 kWh, and the G@S+ block
  gives the latest start date as both 4 and 6 months after signing.

## Tests

- `test_aardgas_variabel_is_priced_at_the_previous_months_index`,
  `test_gas_plus_shares_the_pdf_with_its_own_formula`, `test_august_card`:
  formula, price, fee, VAT and the check at the "vorige maand" index.
- `test_a_card_without_the_product_block_is_refused`, `test_only_flanders`.
- `test_dso_table`: the Antwerpen row in full, "17.59", Kempen's 2,12.
- `test_levies_as_printed`, `test_december_2025_card_and_its_split_figure`.
- `test_parameters_document`, `test_parameters_agree_with_the_cards`.
- `fetch`, `fetch_for_month` (link, month refusal, pre-2025 names,
  transient raise) and `fetch_index`, with the fetch helpers mocked.
