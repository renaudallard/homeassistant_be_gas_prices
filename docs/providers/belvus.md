# Belvus

Module: `custom_components/be_gas_prices/providers/belvus.py`, card reader
shared with the Energy Together brands in `providers/_energy_together.py`
(see [energy_together.md](energy_together.md) for everything the template
reader does). Tests: `tests/test_belvus.py`, fixtures in
`tests/fixtures/belvus/`.

## Where the cards are

- Card: `https://www.belvus.be/public/tariefkaarten/<YYYY-MM>/Tariefkaart_<Product>_GAS.pdf`,
  the folder named by the price month. A folder not uploaded yet answers 404.
- Listing: https://www.belvus.be/historische-tariefkaarten links every
  month since January 2026 for every product, newest first. `fetch` takes
  the newest month the listing links for the product (`listed_months`).
- No probe: the current card's URL is only known from the listing.

## Products and regions

| Contract id | Label | File / card name | Formula (excl. VAT) | Fee €/year |
|---|---|---|---|---|
| `belvus_flex_online` | Belvus Flex Online | FlexOnline | (TTF_RLP x 1,04) + € 3,5/MWh | 50 |
| `belvus_smart_plus` | Belvus Smart Plus | SmartPlus | (TTF_RLP x 1,1) + € 13,98/MWh | 130 |

- Flanders only ("levering van gas aan residentiële klanten in
  Vlaanderen"), indexed monthly on TTF_RLP.
- FlexOnlinePro_GAS and SmartPlusPro_GAS are professional cards and are not
  read. FlowPlus_GAS is listed until March 2026 only. Dynamisch has no gas
  card.

## Two layouts, one reader

- Up to August 2026 the card is a copy of the Energy Together template
  (macOS Quartz print): "**€c 5,87/kWh", "Formule (TTF_RLP x 1,04) +
  €3,5/MWh", both excise labels before both rates, a lone "18,92 0,165"
  line, and "FLUVIUS MIDDEN-" wrapped above "VLAANDEREN 2,24 14,59 0,98
  77,46" (joined before the rows are read).
- From September 2026 it is a Chrome print with letter-spaced headings
  ("T A R I E F K A A R T"): the prices sit above their markers ("6,84" "*",
  "6,77" "**"), the formula reads "(TTF_RLP × 1,04) + € 3,5/MWh", the fee
  "Jaarlijkse abonnementskost * (incl. btw) € 50/jaar", databeheer "€
  18,92/jaar" and transport "€ c 0,165/kW jaar" on their own lines, and the
  distribution rows "Fluvius Antwerpen 2,26 15,68 0,91 83,22".
- Both print the same misassigned Fluvius labels as the template; the rows
  are mapped by position (energy_together.md).
- The card names its product ("FLEX ONLINE GAS", "FlexOnline"), checked
  against the file name's product. No VAT rate is stated ("Tarieven excl.
  btw", "Nettarieven incl. btw"), so `card_vat_rate` is None and the formula
  and "**" price are grossed up at 6%.

## Index

- Belvus publishes no index values (`/indexatieparameters` answers 404).
- The next month's card states the realised value its "**" price uses:
  "laatst gekende waarde van TTF-DAM 8/2026: €61,729/MWh" on the September
  card, "Belpex TTF-DAM 12/2025: €27,652/MWh" on January's, and on April's
  "Belpex TTF-\nDAM 3/2026 €51,264/MWh" with no colon. From October 2026 it
  reads "TTF RLP-M 9/2026: €75,35/MWh". The "**" price is the formula at that
  value, so it is the formula's TTF_RLP.
- `fetch_index` reads the twelve newest Flex Online cards and keeps each
  card's value for the month before its own (`index_value`); a card naming
  another month or none is skipped, and so is a dead link or an unreadable
  card, while a site that is down fails the fetch. Read on 29 September 2026: December 2025
  to August 2026, in 4 to 9 s on a Raspberry Pi. The values agree with Energy
  Together's publication within its two decimals.

## Archive

- `fetch_for_month` builds the folder URL; a 404 is a month with no card, a
  transient failure raises, a card naming another month is refused, a month
  ahead of today (Home Assistant's clock) is not asked for.
- January to March 2026: refused. They reprint a 2025 distribution table
  (Antwerpen 2,1 14,24 0,87 75,6, databeheer 18,56) with the broken row
  "FLUVIUS LIMBURG 2,09 13,32 70,72 70,72". The Energy Together reader
  refuses the card: the row is what tells the 2025 table, which would bill
  a 2026 month on the previous year's network tariffs, and whose rows do not
  follow the positions the reader maps.
- April 2026: refused. Same 2025 table, and the label wraps as "FLUVIUS" /
  "MIDDENVLAANDEREN", so the label order check fails.
- May to September 2026 read.

## Known quirks and card errors

- The tax block is the template's stale one on every card (excise 0,8724 /
  0,9457, contribution 0,1057); see energy_together.md.
- The January to April 2026 network figures are 2025's while Fluvius's 2026
  tariffs applied from 1 January.
- The index footnote named "Belpex TTF-DAM", a power exchange, until April.

## Tests

`tests/test_belvus.py`: the energy leg in both layouts with the formula
check, the card month, both layouts mapping the labels alike (and giving the
same table), the levies as printed, the January card refused, product and
region refusals, `index_value` (September, August, January, a wrong month),
the listing, `fetch`, `fetch_for_month` (folder URL, other month, 404,
transient, future, Brussels time) and `fetch_index` (a dead card link
skipped, a site down failing).
