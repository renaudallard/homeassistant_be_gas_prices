# Entities, services, diagnostics and Repairs

Every entity of an entry hangs off one device named after the entry.

## Sensors

| Key | Unit | Value |
| --- | --- | --- |
| `current_price` | EUR/kWh | The all-in price of one kWh this month. Attributes: `tier`, `annual_kwh`, `annual_kwh_measured`, `index_value`, `index_month`, `price_provisional`, `card_month`, `valid_until`, `card_source` (`live`, `cache` from the Store, `archive` for a row of the card archive, either standing in while the supplier cannot be read or its reading of a card no reader here reads, `ocr` for the card archive's reading of a card published as images), `source_url`, `snapshot_age_hours`, `snapshot_stale`, `last_error`. |
| `current_price_m3` | EUR/m³ | The same per cubic metre at this month's conversion factor. For an Energy dashboard gas source in m³. |
| `energy_component`, `network_component`, `taxes_component` | EUR/kWh | The three parts of `current_price`, VAT inclusive. |
| `fixed_costs_eur_per_year` | EUR | Supplier fee, distribution fixed term, metering and Brussels levy for a year, each an attribute. |
| `supplier_fixed_fee_eur_per_year` | EUR | The supplier's part alone. |
| `conversion_factor` | kWh/m³ | The factor in use and where it comes from. |
| `current_year_cost` | EUR, `TOTAL`, monetary | The year's running bill, each month on its own card; `months` attribute with each month's parts, `unpriced_contracts` naming any earlier contract of the year that could not be priced (its days are left out). |
| `current_month_cost` | EUR, `TOTAL`, monetary | The running month. |
| `year_to_date_consumption` | kWh | What the meter recorded this year, from the contract start when the entry counts the year from it. |
| `projected_year_cost` | EUR | Shown as *Rolling year cost*. The rolling year: the last 365 days' volume at today's price plus a year of fixed costs. |
| `projected_year_end_cost` | EUR | The year so far (from the contract start when the entry counts from it) plus last year's remaining days at today's price. |
| `projected_year_consumption` | kWh | The year so far (from the contract start when the entry counts from it) plus last year's remaining days. |
| `rolling_year_consumption` | kWh | The last 365 days. |
| `contract_end_date` | timestamp | Only with an end date set; removed when it is cleared. |
| `potential_saving` | EUR | Only with the daily ranking on, removed when it is turned off: the cheapest contract's saving against the household's own (on the custom supplier, its typed card, quoted beside the ranking), the ranking as an attribute. |

The running costs report `unknown` until a gas meter is readable. Right after
a restart they show what they showed before it, until the meter is read in the
background; one for a month or year that has ended since, or after a setting
was edited or the gas meter changed, reads `unknown` until then.

## Button

*Refresh tariff card* (diagnostic): fetch the card, the index values and the
calorific values on the next tick.

## Services

- `be_gas_prices.refresh` (`entry_id` optional).
- `be_gas_prices.backfill_statistics` (`entry_id`, `start_date`, `clear`),
  returning the rows written per statistic.

## Repairs

| Card | Raised when |
| --- | --- |
| `snapshot_stale` | The card is a week old or a week past its month. Fixable: fetch again, the card staying while the fetch fails. |
| `extractor_failed` | Two fetches in a row failed for a reason a retry will not fix. |
| `card_unreadable` | The supplier published its card as images and the card archive holds no reading of it for the month. |
| `card_read_by_ocr` | The entry prices on the card archive's OCR reading of a card published as images (Ecofix). Informational; clears by itself when the supplier publishes a readable card again. |
| `meter_unit` | The gas meter cannot be read: its statistics are in a unit that converts neither to a volume nor to energy, or the recorder could not be queried, until a tick reads it again. |
| `several_meters` | The Energy dashboard lists several gas meters and none is set in the options. |

## Diagnostics

The download holds the entry's settings (postcode, meter and station
redacted), the card as parsed, and the computed state.
