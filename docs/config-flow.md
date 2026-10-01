# The config flow

`config_flow.py` holds the setup wizard (`BeGasPricesConfigFlow`) and the
options flow (`BeGasPricesOptionsFlow`). Both walk the same steps, defined
once in `_FlowSteps` over `self._data`.

## Setup steps

| Step | Asks | Notes |
| --- | --- | --- |
| `postcode` (`user`) | Postcode, optional | `postcodes.resolve` gives the region and the gas DSOs of the postcode. Unknown postcode: error. A postcode with no gas DSO designated: error, clear it to pick by hand. Part of it on a DSO no card prices (`UNPRICED`): `unpriced_network`. Blank: `region`. Suggested rather than defaulted, so clearing the field reaches `region`. |
| `unpriced_network` | Nothing | A warning: part of the postcode is on a network no Belgian card prices (Baarle-Hertog's Enexis), so continue only on the postcode's Belgian DSO. |
| `region` | Region | Only when no postcode resolved. |
| `supplier` | Supplier | Suppliers with a contract in the region, withdrawn ones hidden, the custom supplier last. |
| `contract` | Contract, start date, tariff card month, end date, count from start | End before start is refused. Blanked dates are removed. Count from start is not offered while a change of contract is recorded this year, since the year then starts with the earlier contract. |
| `signed_rate` | Price or factor and base, yearly fee | Only when a start date or card month is set, and never for the custom supplier, whose card is already the typed figures; all optional. See `manual_rate.py`. |
| `dso` | DSO | Narrowed to the postcode's DSOs when it resolved. |
| `custom_energy`, `custom_network` | The typed card | Only for the custom supplier. The Walloon fee and the Brussels levy only in their region. |
| `household` | Annual consumption, Brussels meter caliber, gas meter, conversion mode, card archive, daily ranking | The caliber only in Brussels. The conversion mode defaults to the bill's factor. |
| `station` | Reception station | From Atrias's latest published month. If Atrias cannot be read the flow moves to `factor` with an error. |
| `factor` | Conversion factor | kWh/m³ from the bill. Optional and suggested rather than defaulted: left empty, the flow moves to `station`, since a form has no way back to the mode picked on `household`. |

The entry's title is the contract's label. Everything is stored in
`entry.data`; the options flow writes it back and the update listener
reloads the entry.

## The options menu

- **Edit settings**: the setup steps, pre-filled.
- **Compare another contract**: pick a supplier (the custom supplier, with
  no card of its own, is not offered) and a contract; both it and
  the household's own contract are quoted (`compare.quote_contract`) and
  shown side by side. Nothing is saved.
- **Compare every contract**: a progress step ranks every contract sold in
  the region (`compare.rank`, 120 s budget, three cards at a time, one
  supplier's in turn so a page or card they share is read once, and the
  typed card of a custom household among them), then the ranked table with
  the household's own row in bold. Nothing is saved.
- In both tables a dagger marks a provisional price and `OCR` a card
  published as images, quoted on the card archive's reading of it when the
  entry lets the archive be read (`month_cards.current_card`).
- **Record a change of contract**: the first day of the new contract; the
  current settings become an earlier contract ending the day before
  (`contract_periods.record_switch`) and the supplier steps set up the new
  one, without the old contract's card month, end date or typed figures.
  The date must be after 1 January, the current contract's start and the
  last recorded change, so the contract it closes supplied a day of the
  year, and not in the future.
