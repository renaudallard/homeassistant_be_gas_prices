<p align="center">
  <img src="logo.svg" alt="BE gas" width="640"/>
</p>

<p align="center">
  <a href="https://github.com/renaudallard/homeassistant_be_gas_prices/releases/latest">
    <img src="https://img.shields.io/github/v/release/renaudallard/homeassistant_be_gas_prices?label=version&style=flat-square&sort=semver" alt="Latest release"/>
  </a>
  <a href="https://github.com/renaudallard/homeassistant_be_gas_prices/actions/workflows/validate.yml">
    <img src="https://img.shields.io/github/actions/workflow/status/renaudallard/homeassistant_be_gas_prices/validate.yml?style=flat-square&label=hacs%20%2F%20hassfest" alt="Validate"/>
  </a>
  <a href="https://github.com/renaudallard/homeassistant_be_gas_prices/actions/workflows/test.yml">
    <img src="https://img.shields.io/github/actions/workflow/status/renaudallard/homeassistant_be_gas_prices/test.yml?style=flat-square&label=tests" alt="Tests"/>
  </a>
  <a href="https://www.home-assistant.io/">
    <img src="https://img.shields.io/badge/Home%20Assistant-2026.2.3%2B-41BDF5?logo=home-assistant&logoColor=white&style=flat-square" alt="Home Assistant"/>
  </a>
  <a href="https://hacs.xyz">
    <img src="https://img.shields.io/badge/HACS-Custom-41BDF5.svg?style=flat-square" alt="HACS"/>
  </a>
  <a href="./LICENSE">
    <img src="https://img.shields.io/github/license/renaudallard/homeassistant_be_gas_prices?style=flat-square" alt="License"/>
  </a>
  <a href="https://www.paypal.me/RenaudAllard">
    <img src="https://img.shields.io/badge/PayPal-Donate-blue.svg?logo=paypal&style=flat-square" alt="PayPal"/>
  </a>
</p>

---

Home Assistant integration that exposes the **all-in price paid for natural
gas** in Belgium, per kWh and per cubic metre, taking into account every part
of a Belgian gas bill (energy + distribution + transport + levies + VAT), and,
from your gas meter, the **running bill** of the year and of the month.

Prices are read **live** from each supplier's own published tariff card.
**No supplier price is hardcoded in the source.** A supplier is one Python
module that knows where its card is published and how to read it. If yours is
missing, **open an issue asking for it** with the
[supplier request](https://github.com/renaudallard/homeassistant_be_gas_prices/issues/new?template=supplier_request.yml)
form rather than writing the module: a card carries a dozen things that are
easy to read almost right, and getting them wrong mis-prices a bill quietly.

This is the gas sibling of
[Belgian Electricity Prices](https://github.com/renaudallard/homeassistant_be_electricity_prices)
and [Belgian Water Prices](https://github.com/renaudallard/homeassistant_be_water_prices).

> Targets Home Assistant **2026.2.3 or newer**, the version the test suite
> installs.

## Highlights

**Prices**

- **Live tariff cards**: every figure comes from the supplier's own card.
- **Whole-bill view**: energy, distribution, transport, levies and VAT in one
  EUR/kWh, and the same per m³ for an Energy dashboard gas source counted in
  cubic metres.
- **Indexed contracts priced on the supplier's own index**: most variable gas
  contracts are `factor x index + base`, where the index (TTF or ZTP, day-ahead
  or month-ahead, RLP-weighted or not) belongs to the delivery month and is
  only known once the month is over. Each month is priced on the value the
  supplier itself publishes, never on another publisher's figure for "the
  same" market, and a month whose value is not out yet is priced at the
  latest one and marked provisional.
- **The tier and the excise the way the bill applies them**: the distribution
  tier (up to 5 000, 150 000 or 1 000 000 kWh a year) prices every kWh of the
  year, and the federal excise is billed per slice of the annual volume.
- **The levies from the law**: for the delivery months the law is known for,
  the special excise, the energy contribution and the Walloon connection fee
  are what the law sets, whatever a stale or wrong card prints.
- **Cubic metres to kWh**: the factor printed on your bill, or without one
  the monthly calorific value of your gas reception station, published by
  Atrias.

**Costs over time**

- **Year-to-date and month-to-date cost** from your gas meter, each month
  billed on its own card: the project's card archive where it holds the
  month, the supplier's archive otherwise.
- **Rolling year cost** and a **projected year-end cost**.
- **Signing-cohort pricing**: set a contract start date and the card of the
  month supply started prices a fixed or indexed contract (or that of the
  tariff card month, when you signed on another month's card), or type the
  figures of your contract. A variable price is the supplier's for each
  month.
- **Changed contract during the year?** Record the switch and each contract is
  billed on its own supplier's cards for its own days. A switch recorded on
  the wrong day can be removed and recorded again.
- **Price history backfill**: the price sensors' statistics are filled back to
  1 January the first time an entry runs in a year, or to the contract start
  when the year counts from it, or to a change of contract recorded this
  year.

**Choosing a contract**

- **Ranked comparison of every contract** sold in your region for your own
  household, optionally once a day with the best saving as a sensor. An
  indexed contract is quoted at its index value for the month or, until
  its supplier publishes that, the month before, which most cards are set
  at; otherwise at the price its card prints, so the rows stay comparable
  whichever supplier publishes first.
- **One-off quote** of any contract against your own.

**Running it**

- **Self-healing**: the last good card keeps pricing through an outage, a
  restart while a supplier is down prices on the stored card, and the card
  archive stands in when there is none. Repairs cards explain what needs you.
- **Postcode to network operator**, **translated UI** (EN, NL, FR, DE), an
  **expert custom supplier** for a product with no public card.

## Supported suppliers

| Supplier | Contracts | Source |
| --- | --- | --- |
| **Belvus** | Flex Online · Smart Plus *(monthly TTF_RLP)* | Flanders only · [`belvus.py`](./custom_components/be_gas_prices/providers/belvus.py) · [notes](./docs/providers/belvus.md) |
| **Bolt** | Bolt Fixe · Bolt Plenty Fixe · Bolt Variable · Bolt Variable Online · Bolt Plenty Variable · Bolt Plenty Variable Online *(TTF day by day, priced on the monthly RLP-weighted mean Bolt publishes; see the note below the table)* | All three regions · [`bolt.py`](./custom_components/be_gas_prices/providers/bolt.py) · [notes](./docs/providers/bolt.md) |
| **Dots Energy** | Connect - Digital *(a monthly price set on ZTP)*; its card repeats the small tier's term as Fluvius Limburg's mid tier, so a Limburg household above 5 000 kWh gets a pricing error rather than a bill 1,257 c€/kWh too high on distribution until Dots corrects it | Flanders only · [`dots.py`](./custom_components/be_gas_prices/providers/dots.py) · [notes](./docs/providers/dots.md) |
| **EBEM** | Aardgas Variabel · G@S+ *(monthly ZTP-RLP0)* | Flanders only · [`ebem.py`](./custom_components/be_gas_prices/providers/ebem.py) · [notes](./docs/providers/ebem.md) |
| **Ecofix** ⚠️ *(cards are page images, read by the card archive)* | Flexy · Flexy Online *(TTF-RLP-M; Ecofix publishes no index values, so each month is priced at the price its card prints)* | Flanders + Wallonia · [`ecofix.py`](./custom_components/be_gas_prices/providers/ecofix.py) · [notes](./docs/providers/ecofix.md) · **see the note below the table** |
| **Elegant** | Flex · ComfortFlex *(monthly TTFDAM)* · Zeker & Vast | Flanders only · [`elegant.py`](./custom_components/be_gas_prices/providers/elegant.py) · [notes](./docs/providers/elegant.md) |
| **Eneco** | Aardgas Vast · Aardgas Flex · Aardgas Flex One *(monthly TTFDAW-RLP-M)* | Flanders + Wallonia · [`eneco.py`](./custom_components/be_gas_prices/providers/eneco.py) · [notes](./docs/providers/eneco.md) |
| **energie.be** | Gas particulier online *(monthly TTF_RLP)* · Gas vast particulier online | Flanders only · [`energiebe.py`](./custom_components/be_gas_prices/providers/energiebe.py) · [notes](./docs/providers/energiebe.md) |
| **Energy Together brands** | HOA Energy Nova · Prime · APEX Online · Servolt Control · Comfort · Solar · Evident Energie Flexi · Power2You Variabel · Flex · Prijspunten Energie Marktflex · Smappee Smiles Variabel *(all monthly TTF_RLP, one card template)* | Flanders only · [`energy_together.py`](./custom_components/be_gas_prices/providers/energy_together.py) · [notes](./docs/providers/energy_together.md) |
| **EnergyVision** | Goedkope stroom - Gas *(monthly ZTP-RLP-M)* · Goedkope stroom 1 jaar vast - Gas | All three regions, the fixed card Flanders + Wallonia · [`energyvision.py`](./custom_components/be_gas_prices/providers/energyvision.py) · [notes](./docs/providers/energyvision.md) |
| **Engie** | Easy Fixe · Empower Fixe · Easy Variable *(ZTP101, set before the month)* · Flow · Direct Online · Basic Online · Empower Variable · Empty House *(monthly ZTPDAM)* | All three regions, Basic Online Flanders + Wallonia · [`engie.py`](./custom_components/be_gas_prices/providers/engie.py) · [notes](./docs/providers/engie.md) |
| **Frank Energie** | Gas Variabel · HV · JN · Slim · Korting *(monthly M ZTP RLP0N EOD EEX)* | Flanders only · [`frank.py`](./custom_components/be_gas_prices/providers/frank.py) · [notes](./docs/providers/frank.md) |
| **Luminus** | Comfy · Comfy+ · MaxxFix · BasicFix Online · ComfyFlex · ComfyFlex+ *(quarterly TTF DAHW)* · MaxxFlex *(monthly TTF DAH M)* · BasicFlex Online *(monthly TTF DAH RLP M)* | Flanders + Wallonia · [`luminus.py`](./custom_components/be_gas_prices/providers/luminus.py) · [notes](./docs/providers/luminus.md) |
| **Mega** | Smart · Cosy · Online · Prepaid Flex *(monthly ZTP)* · Off-peak Flex · Off-peak Impact *(monthly TTF, Impact in Wallonia only)* · Smart · Cosy · Online · Prepaid · Off-peak · Zen Fixed | All three regions · [`mega.py`](./custom_components/be_gas_prices/providers/mega.py) · [notes](./docs/providers/mega.md) |
| **OCTA+** | Basic Online · Boost Flex · Eco Boost Flex · Smart Variable *(monthly ZTP RLP M)* · Boost Fix · Eco Boost Fix | Flanders + Wallonia · [`octaplus.py`](./custom_components/be_gas_prices/providers/octaplus.py) · [notes](./docs/providers/octaplus.md) |
| **Sparki** | Self Service · At Your Service *(a monthly price set on TTF; Sparki publishes no index values, so each month is priced at the price its card prints)* | Flanders + Wallonia · [`sparki.py`](./custom_components/be_gas_prices/providers/sparki.py) · [notes](./docs/providers/sparki.md) |
| **TotalEnergies** | Gaz Fixe · Gaz Variable · Impact Variable *(Wallonia)* · myComfort Variable · myComfort Fixe · myEssential Variable · myEssential Fixe *(variable ones monthly TTF_M_RLP)* | All three regions · [`totalenergies.py`](./custom_components/be_gas_prices/providers/totalenergies.py) · [notes](./docs/providers/totalenergies.md) |
| **Trevion** | Gas Flex *(monthly TTF_RLP)* | Flanders only · [`trevion.py`](./custom_components/be_gas_prices/providers/trevion.py) · [notes](./docs/providers/trevion.md) |
| **Expert: custom figures** *(no public card)* | A fixed price and fee you type, with your DSO's figures | All three regions · [`custom.py`](./custom_components/be_gas_prices/providers/custom.py) |

> [!WARNING]
> **Ecofix publishes its gas cards as page images.** Nothing in an
> installation can read them, so an Ecofix entry prices on the project's card
> archive, which reads each card once a day with
> [ocr_price_cards](https://github.com/renaudallard/ocr_price_cards), an OCR
> engine that knows the fonts these cards are set in, and files what it read.
> A line on which the engine was not sure of a mark is left out rather than
> guessed, so a figure it could not read leaves the card unpriced rather than
> priced wrong. The Repairs card *card read from its image* says so, and the
> comparison pages mark such a row `OCR`. The federal levies and the Walloon
> connection fee are billed from the law, whatever the card prints. An Ecofix
> entry, or one that left Ecofix this year, needs the card archive option on,
> and the setup does not let it off.

> [!NOTE]
> **Bolt's variable cards bill each day at that day's TTF day-ahead price.**
> Consumption the network operator reports without daily values is spread
> over the days by the Synergrid profile, which makes the month's bill the
> profile-weighted mean of the month, and that is the monthly value Bolt
> publishes on its price list page. A household whose daily values reach Bolt
> is billed on its own days, which this mean only approximates. Bolt issues
> a new variable card on no fixed schedule and a contract keeps the card it
> was signed on, so a month with no card of its own takes the one in force
> then.

**Not supported, and why:**

- **DATS 24** left the residential market on 31 August 2026.

## How the bill is computed

Per kWh:

```
all_in = (energy + tier_proportional + transport + excise + energy_contribution)
         x (1 + VAT) + walloon_connection_fee
```

Per year: the supplier's fixed fee, the tier's fixed term, the metering fee
(Flanders and Brussels) and in Brussels the per-meter levy, accrued by the
day. VAT is 6% on everything but the Walloon connection fee. The details,
with the sources of every regulated figure, are in
[docs/pricing-model.md](docs/pricing-model.md).

## Sensors

All sensors share one device per entry, named after the contract.

| Sensor | Description |
| --- | --- |
| `current_price` | All-in EUR/kWh this month. Attributes: the tier, the annual volume and whether it was measured, the index value and its month, whether the price is provisional, the card's month, its validity, source and URL, its age, whether it is stale and any error. |
| `current_price_m3` | The same per cubic metre, for an Energy dashboard gas source in m³. |
| `energy_component`, `network_component`, `taxes_component` | The three parts of `current_price`, VAT inclusive. |
| `fixed_costs_eur_per_year` | Supplier fee, distribution fixed term, metering and Brussels levy for a year. |
| `supplier_fixed_fee_eur_per_year` | The supplier's part alone. |
| `conversion_factor` | The kWh a cubic metre is worth this month, and where it comes from. |
| `current_year_cost` | The year's running bill, each month on its own card, fixed costs accrued by the day from 1 January (or the contract start when the year counts from it), with a per-month breakdown attribute and the earlier contracts of the year that could not be priced. `TOTAL` and monetary, so the Energy dashboard can use it. |
| `current_month_cost` | The running month's bill. |
| `year_to_date_consumption` | kWh the meter recorded this year, from the contract start when the year counts from it. |
| `projected_year_cost` | Shown as *Rolling year cost*. The last 365 days' volume at today's price plus a year of fixed costs: roughly what a year on this contract costs. |
| `projected_year_end_cost` | This year so far plus last year's remaining days at today's price. |
| `projected_year_consumption`, `rolling_year_consumption` | The corresponding volumes. |
| `contract_end_date` | With an end date set, for a renewal reminder. |
| `potential_saving` | With the daily comparison on: what the cheapest contract would save a year. |

The device also has a **Refresh tariff card** button, which fetches the card,
the index values and the calorific values again, even while the card cannot
be read.

The running costs report `unknown` until a gas meter is readable. The rolling
year needs 350 of the last 365 days on record and the year-end projection
every remaining day of last year, so all four stay `unknown` until the meter
has about a year of history.

Startup reads no meter. Home Assistant waits on each integration's first
refresh, with 300 s for all of them together, and a year of meter statistics
can take seconds on a database on a NAS. So after a restart the cost and
volume sensors show the figures they showed before it, without their
per-month attributes, and the refresh that reads the meter runs right after
startup instead of inside it. A figure for a month or a year that has ended
since, or after a setting was edited or the gas meter changed, reads
`unknown` until then, as every cost does on a new entry. Only a card that
cannot price the volume startup has, held or typed, makes it read the meter,
since the measured volume may fall in a tier the card does price. A refresh
that finds Home Assistant still loading the Energy dashboard's settings,
which name the gas meter, keeps the held figures the same way and leaves the
meter to the next refresh; the daily comparison does not wait for it and
uses the held volume, or the typed one when none is held.

## Installation

### HACS

1. Open HACS, three-dot menu, **Custom repositories**.
2. Add `https://github.com/renaudallard/homeassistant_be_gas_prices` as type **Integration**.
3. Install **Belgian Gas Prices** and restart Home Assistant.
4. **Settings, Devices & services, Add integration, Belgian Gas Prices**.

### Manual

Download the latest [release zip](https://github.com/renaudallard/homeassistant_be_gas_prices/releases),
extract it under `<config>/custom_components/be_gas_prices/` and restart.

`pypdf` and `pdfplumber` are the only extra runtime dependencies;
Home Assistant installs them from the manifest.

## Configuration

1. **Postcode**: resolves the region and your gas network operator. Leave it
   blank to pick both yourself. A postcode with no gas network is refused.
2. **Supplier** and **contract**, with an optional **contract start date**:
   it prices a fixed or indexed contract on the card of the month supply
   started, and can be neither in the future nor before a change of
   contract recorded on the entry. The folded **Advanced** section holds what most households leave
   alone: a **tariff card month** when you signed on another month's card,
   the **end date** for a renewal reminder, counting this year's cost from
   the contract start, and a switch to type the figures of your contract
   when they differ from the card or the card cannot be reached.
3. **Network operator**, only asked without a postcode and outside
   Brussels. The expert custom supplier then asks for the figures of your
   card: the energy price and fee, then your network operator's terms and
   the levies.
4. **Household**: your yearly consumption in kWh (it picks the tier and the
   excise slices until your meter has measured a full year), your meter size
   in Brussels, your gas meter (leave blank to use the Energy dashboard's),
   the **conversion factor** printed on your bill, whether the project's
   card archive may be read (an Ecofix entry prices on it), and whether to
   compare every contract daily.
5. **Reception station**, only when the factor was left empty: its monthly
   calorific value from Atrias converts cubic metres to kWh instead.

**Settings, Devices & services, Belgian Gas Prices, Configure** opens a menu:
edit the settings, quote another contract, rank every contract (today's daily
ranking is shown when the entry made one, with a box to price again), record a
change of contract this year, or remove the last one recorded this year,
which makes the contract left the current one again (the household's
settings stay; an end date it had is entered again in the settings).

## Failure mode

A card that cannot be fetched never stops the pricing. The last good one
keeps serving, from storage across a restart; with none at all, or one that
has gone stale (a week without a successful fetch, or a week past the month it
prices), the project's card archive stands in. Fixing the `snapshot_stale`
Repairs card fetches the card again; `extractor_failed` appears after two
failures in a row that a retry will not fix, and `card_missing` in its place
when the last of them found no card at the address (a 404 or 410, or a web
page served instead), which is a card not published yet or moved rather than
a layout change. A contract its supplier withdrew keeps pricing on the last
card held for it, from storage or the card archive, and the
`contract_withdrawn` Repairs card suggests recording the change of contract
once the supplier moves you.

## How often the integration polls

Every hour, each entry runs one refresh:

- **The tariff card**: the supplier's probe first, a cheap request (a HEAD
  or a small query) that says whether the card changed. The card is fetched
  again when it did, when it is 24 hours old for a supplier without a probe,
  and on every refresh once the month it prices is over, until the next one
  is out. A fetch has 180 s in all; past that, or on a failure, the last
  good card keeps pricing and the next refresh tries again.
- **The supplier's index values**, for an indexed contract, twice a day.
- **Atrias's calorific values**, once a day, only for an entry converting on
  its reception station, and only the months not held yet.
- **Past months' cards**, once each: a month read is kept for good, one no
  archive holds is asked again a day later.
- **The gas meter**, from the recorder's statistics, on every refresh.

With *compare daily* on, every contract is ranked once a day, at a minute
derived from the entry id, so installations do not all fetch every supplier
at once. The refresh button and `be_gas_prices.refresh` fetch the card, the
index values and the calorific values now.

## The card archive

A daily workflow stores every card it parses in the shared
[be_price_cards](https://github.com/renaudallard/be_price_cards) repository
under `gas/`: the parsed card per supplier, contract, region and month, the
text the parse read, the PDF itself as a release asset, and each supplier's
index table. A card published as page images is read there with the OCR
engine, and the row says so; a card it cannot read whole is filed as
`[archive-cards] the OCR could not read a card`. A new version of the
engine or of a PDF reader reads the months it read again, from their kept
PDFs.
The OCR reads a font only through the glyphs it
has learnt, and no Ecofix card it learnt from sets a bold 4, 7, 8, 9, Q, X,
Y or Z, which a gas formula set in bold would need; a card published as
page images still embeds the fonts of the text set over its picture, so
each run looks at the cards it stored and files `[archive-cards] a card
embeds glyphs the OCR library has not learnt` when one carries such a glyph,
naming the card to learn it from. An installation reads the archive for a
past month before the supplier's own archive, for the running month of a card
published as images, as a stand-in when it holds no card or a stale one, and
for an earlier contract's running month while that supplier cannot be
reached. The request names the supplier, the contract, the region and the
month and nothing else; the option in the settings switches it off.

The archive keeps the running month and the twelve before it. A signing
month older than that is read from the supplier's own archive where it still
holds it; otherwise each month is priced on its own card rather than the one
signed for, unless you type the figures of your contract. A card an
installation has read is kept in its store; an update reads it again where
it is still served and keeps the stored one where it is not, so this matters
when the entry is new or its signing date changes.

## Services

```yaml
service: be_gas_prices.refresh           # fetch the card, index and calorific values now
data:
  entry_id: 01H...                       # optional; every entry when left out

service: be_gas_prices.backfill_statistics
data:
  entry_id: 01H...                       # optional; required with clear
  start_date: "2026-01-01"               # optional; 1 January, or the contract start when the year counts from it; never before 1 January of last year
  clear: false                           # true deletes the sensors' statistics in full first
```

## Diagnostics

**Settings, Devices & services, Belgian Gas Prices**, then the entry's menu,
**Download diagnostics**: the entry's settings, the last error, whether the
card was found unreadable, missing or read by OCR, the card as parsed, and
the computed state (the price breakdown, fixed costs, index, conversion,
tier, consumption and costs). The postcode, the gas meter, the reception
station and the meter the entry reads are redacted, so the file can be
attached to an issue as it is.

## Troubleshooting

- **Repairs**: what needs you shows up under **Settings, System, Repairs**:
  a stale card (fixing it fetches the card again), a card that could not be
  read or is missing, one read from its image, a meter that cannot be read
  and several gas meters on the Energy dashboard.
- **Debug logging**: add this to `configuration.yaml` and restart, or turn
  on debug logging from the integration's page, which logs the same:

  ```yaml
  logger:
    default: warning
    logs:
      custom_components.be_gas_prices: debug
  ```

  A card that fails, and the traceback of an error no parser expected, are
  logged as warnings without it.
- **An issue report**: attach the diagnostics and, for a card that fails,
  the log lines of a refresh with debug logging on.

## Known limitations

- **The calorific value is per normal cubic metre.** The DSO also corrects the
  metered volume for the pressure and temperature at the meter, which the
  station value leaves out, so it counts more kWh than the bill: under 1% to
  about 4%, depending on the gas pressure at the meter. The factor printed on
  the bill includes that correction, which is why it is the default. It is a
  fixed figure, though, while a station's calorific value moves by about 0,3%
  in a typical month.
- **Welcome credits are not deducted.** Mega's first-year ristourne, OCTA+'s
  credit note after a year, Luminus's new-customer discounts and Frank's
  cashbacks are not read, so the running cost is the bill before them.
- **Baarle-Hertog outside Zondereigen** is on the Dutch network of Enexis,
  which is not priced. Its postcode resolves to Fluvius Kempen, which serves
  Zondereigen, and the setup warns about the rest.
- **Professional contracts are not offered.** Their cards are priced excluding
  VAT with business excise rates.
- **A card's figures are billed as printed** where no regulated figure
  replaces them, errors included. Known on the October 2026 cards:
  TotalEnergies and Elegant print transport as 0,17 c EUR/kWh against
  Fluxys's 0,16536; EBEM still prints Fluvius Kempen's 2025 small-tier term
  (2,12 c EUR/kWh against 2,27489); Bolt prints ORES's terms 0,091 c
  EUR/kWh low (4,198 and 2,115), RESA's mid tier as 2,259 for 2,529, and
  the Brussels levy of a small meter above 5 000 kWh as 12,54 EUR a year
  for 12,59. Ecofix prints the same ORES terms and RESA mid tier, ORES's
  T3 term as 1,548 for 1,639, and gives RESA ORES's mid-tier fixed term
  (140,93 for 122,05). The daily live check reports each of these as a
  notice while the card prints that figure, and as a failure if it prints
  another; the transport at two decimals it cannot tell from Fluxys's, and
  it does not compare T3 terms. A distribution tier that is not cheaper per
  kWh than the one below it is a figure in the wrong cell and is dropped
  with those above it instead, so a household on them gets a pricing error
  rather than a wrong bill.

## Development

```bash
pip install -r requirements-dev.txt
ruff check .
ruff format --check .
mypy --strict --cache-dir .mypy_cache_strict custom_components/be_gas_prices
mypy --cache-dir .mypy_cache_all tests/ scripts/
pytest tests/
```

`scripts/gate.sh` runs all of these at once against a snapshot of HEAD, which
is what to use before a push.

See [docs/](docs/) for the internals.

## License

BSD 2-Clause. See [LICENSE](./LICENSE).
