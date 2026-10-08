# The pricing model

How a month of gas is billed, as `bill.py`, `pricing.py` and
`providers/_resolve.py` implement it. Every function here is pure: no Home
Assistant, no network.

## One kWh

```
all_in = (energy + tier_proportional + transport + excise + energy_contribution)
         x (1 + VAT) + walloon_connection_fee
```

- `energy` is the supplier's price for the delivery month (see below).
- `tier_proportional` is the distribution tariff's proportional term of the
  household's tier. The tier is chosen by the annual volume and prices every
  kWh of the year, not a slice of it ("de maniere non-cumulative, a
  l'ensemble des kWh", CWaPE): T1 up to 5 000 kWh, T2 up to 150 000, T3 up to
  1 000 000.
- `transport` is Fluxys's national transport tariff, which cards print per
  DSO or once.
- `excise` is the federal special excise, billed per slice of the annual
  volume ("tarief per verbruiksschijf, berekend op jaarbasis"): the first
  12 000 kWh at the lower rate and the rest at the higher one.
  `effective_excise` blends the two into one rate per kWh at the household's
  volume. CWaPE publishes 1,119360 c EUR/kWh for its 17 000 kWh reference
  household from August 2026, which is exactly that blend (a test pins it).
- `energy_contribution` is 0,9978 EUR/MWh excluding VAT up to July 2026 and
  zero for residential gas since 1 August 2026.
- The Walloon connection fee (0,0075 c EUR/kWh) carries no VAT, so it is added
  after VAT.

Residential cards print everything VAT inclusive, so `TaxOverlay.vat_rate` is
0.0 and the multiplier is 1. A formula printed excluding VAT is grossed up by
the card's own rate in its parser. `PriceBreakdown` reports each part VAT
inclusive, so `energy + network + taxes == all_in`.

## The year's fixed costs

```
fixed = supplier_fee + tier_fixed_term + metering + brussels_levy
```

Each accrues by the day over the billed window (`days / days_in_year`). The
supplier's fee follows the energy leg, since it is part of the offer signed.
Metering is Fluvius's data management fee in Flanders, Sibelga's yearly
reading fee in Brussels, nothing in Wallonia. The Brussels levy depends on
the meter caliber, and for the smallest caliber on whether the standardised
annual consumption is above 5 000 kWh. A card whose levy table leaves out
the household's caliber cannot price it, like a card missing its DSO.

## The energy price of a month

| Leg | Price of month M |
| --- | --- |
| `FixedRates` | its price |
| `VariableRates` | the card's printed price |
| `IndexedRates` | `factor x index(M) + base` once the supplier has published the index for M; before that, at the latest value it has published (the month is then *provisional*); with no value published up to M, the card's printed figure, or a signed or typed formula at the index that figure was set at |

An index is always the supplier's own: Engie's ZTPDAM (ICIS Heren), OCTA+'s
ZTP RLP M, TotalEnergies's TTF_M_RLP each come from that supplier's
publication (`SupplierExtractor.fetch_index`), because two publishers of
"the same" market rarely publish the same figure. A quarterly index fills
each month of its quarter. An index set before delivery (Engie's ZTP101, a
month-ahead average) is `settled`: the card prices its month on it and the
printed price is final. A settled month the supplier's table does not hold
yet is still priced at the latest value it holds, and is provisional like
any other. With no table at all, a settled card's printed price is final for
its own month only; another month priced on it is provisional.

Which card's leg (`bill.contract_leg`): the card of month M by default, or
the signing card when the entry names a contract start date or a tariff card
month and the contract is fixed or indexed. A variable price is the
supplier's for each month, so the card of month M sets it whatever the
signing date. A signing card no one serves any more (neither the supplier's
archive nor the project's, which keeps twelve months) leaves each month on
its own card's leg. Figures the household typed from its contract
(`manual_rate.py`) are laid over that leg. Where the supplier has published no
index value up to month M (Ecofix publishes none, Trevion's values begin in
March 2026), the card of month M prints only its own price: the index that
price was set at is read back off it, and the signed or typed formula is
priced at that index.

A comparison (`compare.quote_contract`) prices an indexed leg at the index
value of the month quoted or, while the supplier has not published that,
the month before, which most cards print their price at; failing both, at
the card's printed figure (or the formula at the index read back off it).
A settled leg takes the month quoted only, since its card is set at that
month's own value. An older value is never used: suppliers publish on
their own schedules, so it would put each row on a different month.

## The regulated figures the law sets for the delivery month

`resolve_for_delivery` applies them before a month is billed, whatever month
the card was printed for:

- The special excise from January 2025, known until January 2027: 8,23
  EUR/MWh excluding VAT on the first slice and the quarterly adjustment of
  the second up to July 2026 (9,0782 in January to March 2026, 9,3061 in
  April to June, 9,3315 in July, from the FPS Finance excise tariff), then
  10,31 and 11,16 EUR/MWh from 1 August 2026 (programme law of 30 May 2026).
  Most April to June 2026 cards still printed the first quarter's rate.
  Ecofix, Sparki, Belvus and the Energy Together brands still printed the
  pre-August excise on their September 2026 cards, and OCTA+ and
  TotalEnergies a pre-August energy contribution.
- The energy contribution, 0,9978 EUR/MWh excluding VAT from January 2025 to
  July 2026, zero from 1 August 2026.
- Fluvius's data management fee, 17,85 EUR/year excluding VAT for every area
  in 2026, where a Flemish card prints none (Luminus's 2026 cards), the
  custom supplier's typed card with metering left at 0 included.
- The Walloon connection fee, 0,000075 EUR/kWh (0,0075 c EUR/kWh) below
  1 GWh a year, VAT exempt, set by the Walloon order of 19 June 2003 (art. 2)
  and in force since 15 July 2003, on a card of the Walloon DSOs.
  EnergyVision's September 2026 Walloon cards print the low-voltage
  electricity rate, 0,00075, instead.

Each carries its known window in `const.py`; past it the card is read as
printed. Only residential cards are touched.

## The running costs

`running_costs` walks the window (1 January, or the contract start when the
entry bills from it) month by month: the meter's kWh of the month's days,
billed on the month's own card, or on the current card when the month's own
card cannot be read or cannot price the household. Earlier contracts of the
year recorded at a switch are walked the same way on their own suppliers'
cards (`contract_periods.py`) and added: each from 1 January or the day
after the one before it, or from its own contract start when the entry
billed from it, on its own signing card and typed figures (a contract on
the custom supplier on the card typed for it). Their last month's card is
their current card: a month without a card of its own is billed on it, or
on today's card when it is not held or cannot price the household.

- `rolling_year_kwh`: the last 365 days, up to 15 missing days scaled across;
  more missing and it is unknown. Gas is too seasonal for a shorter window to
  be scaled to a year.
- A measured rolling year replaces the typed annual volume for the tier and
  the excise slices.
- `projected_year_end_cost`: the year so far as billed, plus last year's
  same remaining days at today's price and the remaining days' fixed costs;
  unknown unless every one of those days is on record.

## Cubic metres and kWh

A meter in kWh needs nothing. A meter in m³ is converted per month: by the
factor typed from the bill, the default, or by the calorific value of the
household's reception station for that month (Atrias, kWh per m³(n)), the
latest published before it while the month's own is not out. A billed month
without any is no bill at all; a day of last year without any, as before a
station Atrias lists only since a later month, is left out of the rolling
year and the projection, which take it as a missing day. The station
value is per normal cubic metre and leaves out the DSO's correction for the
pressure and temperature at the meter: Fluvius takes 9 °C without a volume
converter, which alone is 273,15 / 282,15 = 0,968, and the pressure terms
only take back part of it (Pa 1016,20 mbar, PHgos 1,54 to 10,74 mbar, and a
gas pressure at the meter taken between 0 and 25 mbar, the one Fluvius uses
not verified), so the station value bills under 1% to about 4% more kWh than
the bill's factor, which includes the correction. That factor is fixed,
while a station's value moves by a median 0,29% from its own mean (1,0% at
the 95th percentile, 60 stations over January 2025 to August 2026).
