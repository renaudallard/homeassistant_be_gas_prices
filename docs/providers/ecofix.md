# Ecofix

Module: `custom_components/be_gas_prices/providers/ecofix.py`.
Tests: `tests/test_ecofix.py`, fixtures in `tests/fixtures/ecofix/`.

## Where the cards are

- One card per product at a stable URL, overwritten in place every month:
  `https://portal.ecofixgp.be/docs/prices/current/GAS_Ecofix_<Product>_NL.pdf`,
  `<Product>` being `Flexy` or `Flexy_Online`.
- The probe is the card's Last-Modified (Azure blob storage also sends an
  ETag). The card of September 2026 was uploaded on 31 August at 11:30 GMT.
- No archive of past cards: the blob is replaced. Past months come from the
  project's card archive.

## Products and regions

| Contract id | Label | Kind | Regions |
|---|---|---|---|
| `ecofix_flexy` | Ecofix Flexy | indexed (TTF-RLP-M) | Flanders, Wallonia |
| `ecofix_flexy_online` | Ecofix Flexy Online | indexed (TTF-RLP-M) | Flanders, Wallonia |

The Dutch card prints the Flemish and the Walloon tables both, and is read
for either region; the French card carries the same figures and is not read.
There is no Brussels table and no dynamic gas product (the Motion files
answer 404 BlobNotFound).

## Page images, and the OCR engine

Since August 2026 the cards are page images. Their text layer holds the month,
the monthly and yearly prices and the fee, 60 characters over two pages,
which is under the reader's text-layer floor, so `fetch` raises
`CardNotReadableError`.

The card archive's daily walk reads such a card with
[ocr_price_cards](https://github.com/renaudallard/ocr_price_cards), an OCR
engine built for the fonts these cards are set in (Product Sans), and parses
its trusted text with this module: a line on which the engine refused a mark
is left out, so a figure is read whole or missing, and a missing mandatory
figure fails the parse. The row records the engine version it was read with.
An entry prices on the archive's row for the running month
(`month_cards.current_card`) and raises the `card_read_by_ocr` Repairs card.

The engine learnt the Bold glyphs of these cards from the Dutch cards of July
2025, January and February 2026 that the Wayback Machine kept, which still
carried a text layer. Read with it, the September 2026 cards refuse only the
title's `y`, whose descender the export clips, and an edge of a gradient,
neither on a line this module reads. The engine holds no Bold 4, 7, 8 or 9:
no card it learnt from sets one, so a formula printed with those digits is
refused until such a card is learnt.

Because the title line is refused, the product is the one the URL names.

## How each figure is read

The text is the engine's pdfplumber-shaped reading (the fixtures
`GAS_Ecofix_*_NL_2026-09.ocr.txt` are what it produced for September 2026).

- Card month: "Variabele prijzen aardgas voor contracten afgesloten in
  september 2026".
- VAT: "(Prijzen inclusief 6% BTW)" gives `card_vat_rate`.
- Fee and price: "60,00 + 7,23 Maandprijs", the fee in EUR a year and the
  monthly price in c EUR/kWh, VAT inclusive. Up to July 2026 the card printed
  only an expected yearly price ("60.00 3.49 Verwachte jaarprijs" in
  January), and such a card is refused.
- Formula: "Onze tariefformule voor aardgas (excl. btw): (0,1010*TTF-RLP-M)
  + 0,5200 c€/kWh" (0,2200 on Flexy Online), c EUR/kWh excluding VAT against
  the index in EUR/MWh, grossed up by the card's 6%.
- Flanders table, between "Vlaanderen Distributie" and "Wallonië
  Distributie": per tier the fixed term then the proportional one, then data
  management and transport: "Fluvius Kempen 16,17 2,275 85,83 0,882 580,25
  0,552 18,92 0,165".
- Wallonia table, after "Wallonië Distributie": the five ORES rows, which
  agree and collapse, and "TECTO - RESA", without data management.
- Levies: "Bijdrage op energie 0,10577", "Verbruik tussen 0 & 12.000 kWh
  0,87238", "Verbruik groter dan 12.000 kWh 0,96229", and in Wallonia
  "Aansluitingsvergoeding 0,00750".

## The index

TTF-RLP-M: "het rekenkundig gemiddelde van de dagelijkse Day Ahead- en Weekend
TTF-prijzen (EGSI) tijdens de maand van levering, zoals gepubliceerd op de
website van EEX". Ecofix's tariff page links no index values, and EEX's terms
forbid reuse, so there is no `fetch_index`: every month is priced at the
"Maandprijs" its card prints and marked provisional. A contract signed on an
earlier card, or with typed figures, is priced on its own formula at the
index the month's card was taken back to (`bill.contract_leg`).

The two products are priced on one index value, which neither card names.
Taken back through each formula, the printed prices give 62,38 and 62,37
EUR/MWh in September 2026, within the rounding of two decimals; a test holds
them to it, which a misread digit of a base would break by several EUR/MWh.

## Known quirks and card errors, read as printed

- The pre-August federal levies (excise 0,87238 / 0,96229, contribution
  0,10577) on the September card. The law replaces them for the months it
  is known for.
- ORES terms 4,198 (T1) and 2,115 (T2) c€/kWh, as Bolt prints them, where
  every other card prints 4,289 and 2,206.
- RESA's mid-tier fixed term 140,93, which is ORES's; the other cards print
  122,05.
- "TECTO - RESA" for TECTEO - RESA.

## What the tests pin

- Both products: price, fee, factor, base and formula; the two formulas
  pricing their cards at one index value; month and VAT.
- Flanders: the Kempen row in full, no connection fee. Wallonia: the ORES
  collapse and the RESA row with its card error, the levies as printed.
- A line the engine left out fails the parse; the January 2026 card (the
  layout before the monthly price) is refused; Brussels and an unknown
  contract are refused.
- The published September cards are page images to the reader here.
- `fetch` reads the Dutch card for Wallonia, the probe is the Last-Modified,
  and there is neither an index table nor an archive of Ecofix's own.
