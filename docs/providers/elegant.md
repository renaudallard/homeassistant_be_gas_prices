# Elegant

Module: `custom_components/be_gas_prices/providers/elegant.py`.
Tests: `tests/test_elegant.py`, fixtures in `tests/fixtures/elegant/`.
There is no electricity counterpart for Elegant.

## Where the cards are

- Listing: https://www.elegant.be/tariefkaarten, server-rendered Next.js.
  It links the current residential cards as DatoCMS assets:
  `https://www.datocms-assets.com/198110/<upload id>-<product>gas_residential-<MMYY>.pdf`.
  The upload id is opaque, so the listing is read. Where a product is linked
  more than once the newest `MMYY`, then the highest upload id, wins.
- Probe: the listing's ETag.

## Products and regions

Flanders only ("Sinds 2012 voor iedereen in Vlaanderen").

| Contract id | Label | Kind | File token | Archive key |
|---|---|---|---|---|
| `elegant_flex` | Elegant Flex | indexed (monthly TTFDAM) | `flexgas` | `FlexGas` |
| `elegant_comfortflex` | Elegant ComfortFlex | indexed (monthly TTFDAM) | `comfortflexgas` | `ComfortFlexGas` |
| `elegant_zeker_vast` | Elegant Zeker & Vast | fixed, one year | `zekervastgas` | `ZekerVastGas` |

## How each figure is read

Reader: pdfplumber layout (`fetch_pdf_text_layout`); pypdf separates the
DSO labels from their figures.

- Heading: "Tariefkaart Aardgas", the product name on its own line
  ("Flex", or "Flex 09.26" on an archived card), then "... Versie
  `<maand> <jaar>` Particulier ...". The name and "Particulier" must match
  the contract: Flex and ComfortFlex print the same figures, so nothing
  else tells the two cards apart.
- Fee: "Vaste vergoeding (€/jaar)(1) 75,00".
- Price: "(c€/kWh) 7,20 Maandelijkse prijzen". The "Geschatte jaarprijs"
  line below it (the VREG estimate) is not read.
- Formula: "(1,0250 x TTFDAM + 0,470) x 1,06" in c€/kWh against the index
  in c€/kWh, VAT inside. `factor = 1,0250 x 1,06 / 1000` per EUR/MWh,
  `base = 0,470 x 1,06 / 100`.
- VAT: "Prijzen inclusief 6% btw".
- DSO table, after "Nettarieven": per tier "vast (€/jaar)" then
  "var (c€/kWh)", T1 and T2 only: `Fluvius Antwerpen 15,68 2,26 83,22 0,91`.
  "Fluvius Midden-Vl." is too far from the full name for the fuzzy match,
  so it has its own label.
- Printed once below the table: "Tarief databeheer 18,92 €/jaar" and
  "Transportkosten 0,17 c€/kWh".
- Taxes: "Energiebijdrage 0,00000 c€/kWh" (optional), "Federale Accijns
  Verbruik 0 - 12000 kWh 1,093 c€/kWh" and "Verbruik > 12000 kWh 1,183
  c€/kWh".

## The index

The card's footnote names the last known value ("de waarde van augustus
2026. Deze bedroeg 6,166 c€/kWh") and refers to elegant.be/indexatie. That
page is a client-side form over two tRPC calls, sent in the batch form the
site itself uses:

- `GET /api/trpc/energyExchanges.listIndexes?batch=1&input={"0":{"energyType":"Gas"}}`
  lists the gas indices by name and id; TTFDAM is id 2 today. The id is
  looked up by name on every fetch.
- `GET /api/trpc/energyExchanges.getRates?batch=1&input={"0":{"exchangeId":2}}`
  returns one row per delivery month from August 2022, `startDate` 06:00
  on the 1st to the 1st of the next month, `price` in EUR/MWh (61.6623 for
  August 2026, the card's 6,166 c€/kWh).

Only rows spanning exactly one calendar month are kept. The answer filtered
on `"year":2026` ends with a one-day row for 1 January 2026 carrying
December's 27,65, which would otherwise replace January's 34,26.

Index name in the snapshot and the table: `TTFDAM`.

## Archive

The tarief-archief page's tRPC call:

- `GET /api/trpc/tariefArchief.search?batch=1&input={"0":{"date":"YYYY-MM-01","energyType":"Gas","customerType":"Residential"}}`
  returns `productOffers[]` with `productOfferKey` (`FlexGas_20260301`),
  `name` and `tariffChartUrl`.
- `GET /api/tarief-archief/tariff-chart?tariffChartPath=<tariffChartUrl, URL-encoded>`
  returns the PDF.

`fetch_for_month` asks for the first of the month: Zeker & Vast is
reissued when its price moves (March 2026 has offers dated the 1st, the 6th
and the 21st), and the card in force on the 1st is the one taken, where
`fetch` reads whatever the listing links today. Before 2026 the offers are
other products (`BEGreenFlexGas`, `BEComfortFlexGas`, `BESmartGas`); the
one checked, BEGreenFlexGas for June 2025, is a combined electricity and
gas card headed "Periode november 2024". Those months return None. A future
month returns no offers.

## Known quirks

- Figures are rounded: transport 0,17 (Fluxys 0,16536), excise 1,093 and
  1,183 (1,09286 and 1,18296), DSO terms to two decimals. Read as printed.
- The archived cards print the name with a month suffix ("Zeker & Vast
  08.26") that the live cards do not.
- TTFDAM is Elegant's own assessment: 61,66 for August 2026 where OCTA+'s
  TTF RLP is 61,729 and Trevion's 61,72.

## What the tests pin

- Flex and ComfortFlex: price, fee, factor, base, the formula check at
  61,66, month, VAT.
- Zeker & Vast as fixed.
- DSO table: all eight areas, Antwerpen in full, Midden-Vl., transport and
  metering from their lines.
- Levies, and the March 2026 archived card with its old levies and its own
  formula check at 33,18.
- The Flex card refused as ComfortFlex; another region refused.
- Listing links for all three products; `fetch`.
- `fetch_for_month`: the query and the chart URL, another product's card
  behind the offer, another month, the legacy products, a transient
  failure.
- The index: id by name, rates by month, the one-day row ignored,
  `fetch_index` calling both endpoints in order.
