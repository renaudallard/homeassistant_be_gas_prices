# energie.be

Module: `custom_components/be_gas_prices/providers/energiebe.py`. Tests:
`tests/test_energiebe.py`, fixtures in `tests/fixtures/energiebe/`.

## Where the cards are

- Current cards: https://www.energie.be/api/v1/data/contracts, the JSON the
  site itself reads. The gas card of a product is
  `contracts[tariffType].contractTypeNgRes.tariffDocument`, tariffType
  `Variable` or `Fixed` (`contractTypeElRes` is the electricity card). The URL
  is an opaque blob, e.g.
  `https://energie.blob.core.windows.net/cms/assets/Energie_be_Gas_Particulier_18ddc53692.pdf`.
- There is no gas document key. The Dynamic contract has no gas card: the
  site says a gas contract beside it "is standaard variabel".
- No probe: the contracts endpoint answers HEAD with a 405, and the blob's
  Last-Modified is only reachable after reading it. Whether the blob name
  changes with each new card was not checked.

## Products and regions

| Contract id | Label | tariffType | Kind | Regions |
|---|---|---|---|---|
| `energiebe_variable` | energie.be Gas particulier online | Variable | indexed, monthly | flanders |
| `energiebe_fixed` | energie.be Gas vast particulier online | Fixed | fixed | flanders |

- "Tariefkaart geldig voor de levering van aardgas aan particuliere klanten
  in Vlaanderen"; the DSO table is Fluvius only.
- Both run one year ("De Leveringsovereenkomst heeft een duurtijd van 1
  jaar").

## How each figure is read (pdfplumber layout text)

- Card month and product: the title line, "Gas particulier online –
  september 2026" or "Gas vast particulier online – september 2026" ("online"
  is missing on the December 2025 cards, "particulier" on the October 2026
  ones: "Gas online – oktober 2026", "Gas vast online – oktober 2026"). The
  word "vast" must match the contract asked for, and the variable card must
  print a formula and the fixed card "De energieprijs is een vaste prijs"
  and no formula.
- Price: the figure opening the line under "Energieprijs" ("6,88 formule
  ..." or "8,06 looptijd ..."), c EUR/kWh incl. VAT.
- Fee: "Vaste vergoeding" / "35" / "(€/jaar)", yearly.
- Formula (variable): "formule (excl. btw): (1,014x TTF_RLP+ 0,16) c€/kWh".
  TTF_RLP is in c EUR/kWh, so with the index in EUR/MWh factor = 1,014 / 1000
  x 1,06 and base = 0,16 / 100 x 1,06.
- VAT: the card says "Alle prijzen zijn inclusief btw tenzij anders vermeld"
  and never states the rate, so `card_vat_rate` is None and the formula is
  grossed by `const.VAT_RATE_REDUCED`. The printed price at the printed VNR
  estimate confirms the 6%.
- DSO table, "Nettarieven" to "Taksen en heffingen": Klein verbruik Variabel
  (c EUR/kWh), Vast (EUR/year), Gemiddeld verbruik Variabel, Vast, Transport
  (c EUR/kWh, 0,1654 on every row), Databeheer (EUR/year). No T3. Labels
  "Fluvius (Antwerpen )" and so on, matched against the shared
  `FLUVIUS_LABELS` (the brackets and stray spaces score above 0,91, the next
  best row below 0,73).
- Excise: "Bijzondere accijns op Energie (c€/kWh) kleiner dan 12.000kWh
  1,0929" and "groter dan 12.000kWh 1,1830", read as printed.
- Energy contribution: "Bijdrage op de Energie (c€/kWh) 0", mandatory. The
  cards before August 2026 print "0, 1058" with a stray space, which reads as
  0,1058.

## Index

- TTF_RLP: "de RLP-gewogen TTF dagprijzen op de TTF Day Ahead Market",
  the delivery month's mean in c EUR/kWh. Known after the month, so
  `settled` is False, and the card prints the formula at a VNR estimate
  ("Weergegeven prijs op basis van de ingeschatte index (VNR methodologie):
  6,24 c€/kWh"). That estimate is the `price`.
- The factor changes from card to card: 1,025 from December 2025 to August
  2026, 1,014 in September 2026. March 2026 printed a base of 0,10 instead
  of 0,16.
- `fetch_index` reads https://www.energie.be/api/v1/data/document?key=Indexation,
  a 302 to `Indexation_Parameters_<hash>.pdf`. Its TTF_RLP table ("TTF_RLP
  (c€/kWh)", years 2024 2025 2026 across, months down) is read with pypdf,
  which yields one cell per line and keeps every figure whole; pdfplumber
  splits some of the Belpex ones ("11,2 1"). Values are c EUR/kWh with two
  decimals, times 10 into EUR/MWh (August 2026: 6,17, so 61,7).
- A month the latest year has not reached is a shorter row, so values fill
  the years from the left. A figure that does not look like "d,dd" fails the
  parse rather than shift a column.

## Archive

- https://www.energie.be/api/v1/data/tariff-cards?isProfessional=false&tariffType=Variable
  (or `Fixed`): `tariffCards[]` with `date` "YYYY/MM" and `gasDocument`.
  34 months, November 2023 to August 2026, published in arrears: the running
  month is never on it, and a month it does not list is None.
- The cards of January to November 2025 are page images (9 characters of
  text), so `CardNotReadableError`, so None; so are the June and December
  2024 cards checked. The November 2023 card has text but prints a formula
  in EUR/MWh ("1,030 x TTF_RLP+ €5,5/MWh"), the ten pre-2025 Fluvius areas
  and an older title ("Gasparticulier –november2023"), and is refused. The
  readable archive is December 2025 to August 2026, both products.
- No clock is involved; a card naming another month than its row is refused.

## Known quirks and card errors

- The printed price and the printed VNR index are both rounded to two
  decimals. On the ten readable variable cards the formula at the printed
  index lands within the price's own rounding (0,005 c EUR/kWh) on seven,
  and 0,006 to 0,009 off on December 2025, January 2026 and March 2026,
  which the index's rounding (up to 0,0054 once through the formula)
  accounts for.
- Fluvius West T2 proportional prints 1,05 where EBEM and Frank print 1,06
  and Fluvius's 2026 tariff is 1,05500: a rounding difference.
- pypdf breaks the card's figures across lines ("6,\n88", "1,0\n14"), so the
  cards are read with pdfplumber's layout text.

## Tests

- `test_variable_card_reads_the_formula_and_its_vnr_price`: formula, price,
  fee, VAT basis and the check at 62,4 EUR/MWh (September).
- `test_the_october_card_leaves_particulier_out_of_its_title`.
- `test_the_factor_is_read_per_card`: August's 1,025 and its 5,55 estimate.
- `test_fixed_card`, `test_card_of_the_other_product_is_refused`.
- `test_dso_table`: the Antwerpen row in full, Kempen and West cells.
- `test_levies_as_printed`, `test_december_2025_card` (no "online", "0,
  1058"), `test_pre_2025_layout_is_refused`, `test_only_flanders`.
- `test_index_document`, `test_the_realised_index_reprices_the_august_card`
  (6,87 at the realised 6,17 against 6,20 printed on the estimate).
- `fetch`, `fetch_for_month` (row URL and query, month refusal, missing row,
  page-image card, transient raise) and `fetch_index`, with the fetch helpers
  mocked.
