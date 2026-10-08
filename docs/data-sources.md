# Data sources

Everything the integration reads besides the suppliers' cards.

## Index values

Each supplier with indexed products publishes the values its bills settle
on, and `SupplierExtractor.fetch_index` reads that publication: Engie's
indexation page, OCTA+'s parameters PDF, TotalEnergies's history PDF, Eneco's
indexation PDF, Mega's indexation page, Luminus's indexation PDF, the series
on Bolt's price list page, and so on
(see each [provider page](providers/)). The table is fetched twice a day and
kept in the entry's Store. A supplier's values are never used for another
supplier's formula: publishers of "the same" hub disagree in the second
decimal (ICIS Heren, EEX and Argus assess it independently) and an RLP
weighting moves it again.

## Calorific values (Atrias)

`calorific.py` reads the monthly gross calorific value of every gas reception
station from Atrias:

- the public API key comes from `https://www.atrias.be/runtime-config.js`;
- `GET https://api.atrias.be/roots/folder/list?folder=SectorData/02 Gross Calorific Values`
  lists the published months, one file per month, published in the first
  days of the next month;
- `GET https://api.atrias.be/roots/download/<path>` returns the month's CSV
  (station name, EAN, value in kWh/m³(n)), whose format varies from file to
  file and is read tolerantly.

`api.atrias.be` does not send its intermediate certificate. The public
"Go Daddy Secure Certificate Authority - G2" intermediate ships in `certs/`
and is added to an otherwise standard client context; verification is never
turned off.

## The card archive

The `archive_cards` workflow stores every card it parses daily in the
be_price_cards repository, under `gas/cards/<supplier>/<contract>/<region>/<YYYY-MM>.json`.
A card published as page images (Ecofix's) is read there with the OCR engine
[ocr_price_cards](https://github.com/renaudallard/ocr_price_cards), and each
source it read records the engine version (`"ocr"` in `_sources`).
An installation reads the archive (`month_cards.fetch_archived_card`, and
`fetch_archived_row` for the OCR flag) for a past month, before the
supplier's own archive, which costs a PDF and a parse per month, for the
running month of a card published as images
(`month_cards.current_card`), and when its current card cannot be read at
all or has gone stale. The request names the supplier, the contract,
the region and the month and nothing else; the *Read the project's card
archive* option switches it off.

## The recorder

`gas_meter.py` reads the gas meter's long-term statistics, daily `change` in
local days, converted by the recorder to m³ (a volume meter) or kWh (an
energy meter). A unit Home Assistant cannot convert is refused rather than
read as m³. A negative daily change is a replaced or re-based `total` meter
and counts as none.

## Postcodes

`scripts/refresh_postcodes.py` builds `postcode_map.py` from bpost's
postcode list and Synergrid's DSO lookup: for each of the 1 146 postcodes
with a province, the gas DSO of every locality Synergrid files under it.
Where Synergrid disagrees with Fluvius's open data, the Flemish regulator or
CWaPE's per-postcode table, `OVERRIDES` in the script corrects the entry,
each with its evidence in a comment (Voeren has no gas network although
Synergrid names Fluvius Limburg; Andenne is RESA, not ORES; eighteen Walloon
postcodes Synergrid leaves without gas are ORES). Run it once a year: DSOs
merge and communes move between them.

`postcodes.resolve` gives the region and the postcode's DSOs, and the setup
flow offers only those. A postcode with no designated gas DSO (260 of them,
nearly all in Wallonia) is refused with `no_gas_network`; clearing the
postcode lets the household pick region and DSO itself.

A gas DSO Synergrid names that has no key goes to `UNPRICED` in the
generated module instead. The one there is Enexis in Baarle-Hertog (2387):
the postcode resolves to Fluvius Kempen, which serves Zondereigen, and the
setup flow's `unpriced_network` step warns that the rest of the commune is on
a Dutch network the integration does not price.
