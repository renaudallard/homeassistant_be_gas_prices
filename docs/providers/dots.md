# Dots Energy

Module: `custom_components/be_gas_prices/providers/dots.py`. Tests:
`tests/test_dots.py`, fixtures in `tests/fixtures/dots/`.

## Where the card is

- Product page: https://www.dotsenergy.be/dots-connect-gas-digital links the
  current card as an Odoo attachment,
  `/web/content/<id>?unique=<checksum>&download=true`. Each month's card
  gets a new id (98918 for September 2026, "Tariefkaart NG Dots Connect -
  Digital 09_2026.pdf"; 110896 for October 2026, "... 10_2026.pdf") and the
  superseded id keeps serving its old card. `card_path` requires exactly one
  attachment link.
- The home page is not an equivalent source: in October 2026 it linked six
  attachments, both months' gas cards and the Dots Dynamic and Dots Dynamic
  Insights electricity cards for September and October. Only the product
  page names the current card.
- No archive of past cards was found, so there is no `fetch_for_month`.
- No probe: the id changes every month, so a HEAD on a known id never sees a
  new card.

## Products and regions

| Contract id | Label | Kind | Regions |
|---|---|---|---|
| `dots_connect_digital` | Dots Connect - Digital | variable | flanders |

- "Variabele prijzen gas voor residentiële klanten in het Vlaamse Gewest",
  open-ended ("Duurtijd contract = onbepaald").
- Not registered: "Dots Internal" (`/internal-gas`) "is exclusief
  voorbehouden voor werknemers van Dots energy BV" and needs Dots's approval;
  "Smart-E-Grid: Gas Legacy" (`/seg-legacy-gas`, still the April 2026 card) is
  reserved for Smart-E-Grid BV customers. The parser refuses a card that is
  not "Dots: Gas Connect - Digital".

## Why VariableRates

The formula line is "Gas 1,05 * M ZTP rek. gem. EGSI EEX + 0,9 7,873" under
the column "M-1 *", in c€/kWh. The card defines its index two contradictory
ways:

- the "*" footnote: "Prijs M-1 vertegenwoordigt het maandelijks rekenkundig
  gemiddelde “settlement price” van de notering "EEX Gas Futures Month Base
  ZTP" ... voor de maand voorafgaand aan de maand van levering", a
  month-ahead futures average, and "Deze indicatieve energieprijzen zijn
  exclusief volumeweging";
- the next paragraph: "de indexatieparameter ZTP rekendig gemiddelde
  gedefinieerd is als het opgemeten verbruik gebaseerde gewogen gemiddelde
  van de day-ahead baseload ZTP-uurprijzen", a consumption-weighted
  day-ahead mean;
- while the formula's own name reads as the arithmetic mean of EEX's ZTP
  spot index (EGSI).

Dots publishes no index values (nothing on its pages or sitemap). Nor does
the 7,873 reproduce from a published ZTP figure: at Engie's ZTP101 for
September (61,768 EUR/MWh) the formula gives 7,829 with VAT or 7,386
without; 7,873 needs 62,17 EUR/MWh if it includes VAT, 66,41 if not. So the
energy leg is `VariableRates(price=0,07873, yearly_fixed_fee=63,60,
formula)`, the price as printed and VAT inclusive by the card's "Alle prijzen
zijn inclusief 6% BTW (indien van toepassing)".

## How each figure is read (pypdf text)

- Month: "Tariefkaart september 2026".
- Energy: the "Gas ... ZTP ... + 0,9 7,873" line (formula up to its adder,
  then the price, on the same line: a price wrapped off it refuses the card
  rather than reading the adder); fee "€5,3/maand/EAN" x 12.
- VAT: "inclusief 6% BTW" into `card_vat_rate`.
- Taxes: "Energiebijdrage 0,10577", "Verbruik tussen 0 & 12.000 kWh 1,093",
  "Verbruik > 12.000 kWh 1,183". pypdf breaks some of these around the comma
  ("0, 10577", "1 ,093"); the regexes allow it.
- DSO table: per tier "Vaste vergoeding €/jaar" then "Afname c€/kWh" for
  0 - 5000, 5001 - 150000 and 150001 - 1000000 kWh/jaar, then "Tarief
  databeheer Jaarlijks €/jaar" and "Transport c€/kWh". Labels are
  alphabetical and correct ("Fluvius Antwerpen 15,68 2,256 83,22 0,906
  562,63 0,586 18,92 0,165").

## Known quirks and card errors

- Wrong cells, each repeating the tier before it, so `dso_overlay` drops
  the tier with those above it (Engie's and Sparki's September cards agree
  with each other on every one of them):

  | Row | Cell | Dots | Engie, Sparki |
  |---|---|---|---|
  | Fluvius Limburg | T2 c€/kWh | 2,240 (its T1 value) | 0,983 (regulator 0,98293) |
  | Fluvius Halle-Vilvoorde | T3 c€/kWh | 0,980 (its T2 value) | 0,621 |
  | Fluvius Kempen | T3 c€/kWh | 0,882 (its T2 value) | 0,552 |
  | Fluvius Midden-Vlaanderen | T3 c€/kWh | 0,910 (its T2 value) | 0,577 |

  The Limburg one matters: read as printed it would price a Limburg
  household between 5 000 and 150 000 kWh 1,257 c€/kWh too high on
  distribution, so that household gets a pricing error until Dots corrects
  its card. The three T3 cells concern no household.
- The energy contribution is still printed (0,10577) beside the new excise
  (1,093 / 1,183, the law's 1,09286 / 1,18296 rounded). The law override sets
  the contribution to zero from August 2026.
- pdfplumber jumbles the header cells and reverses a rotated label
  ("nerednaalV"); pypdf reads the card cleanly.

## Tests

`tests/test_dots.py`: the price kept as printed, the levies, the Antwerpen
row, the wrong cells dropping their tier, the Internal card refused, region and
contract refusals, a lost price line, a price wrapped off the formula line,
the product page link, `fetch`, and the
extractor having neither archive nor index.
