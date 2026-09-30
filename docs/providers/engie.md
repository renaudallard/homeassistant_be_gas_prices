# Engie

Module: `custom_components/be_gas_prices/providers/engie.py`.
Tests: `tests/test_engie.py`, fixtures in `tests/fixtures/engie/`.

## Where the cards are

- The public document endpoint Engie's own site uses, one card per product
  and region:
  `https://www.engie.be/api/engie/be/ms/pricing/v1/public/pricesAndConditionsPDF?document=<CODE>&monthOffset=0&segment=R&language=F`.
- A gas code reads `G_<FAMILY>_R_GREY_C_<F|I>_<MM>_<V|W|B>_F`: every gas
  family is GREY, `F` is fixed and `I` indexed, `MM` the contract term as
  Engie writes it (`00` for an open-ended one) and the last letter the
  region. The term differs by region for some products (EASY is 12 months
  in Flanders and Wallonia, 36 in Brussels), so each contract carries its
  term per region.
- There is no probe: the card is fetched on the coordinator's time-based
  schedule.

## Products and regions

| Contract id | Label | Code family | Kind | Regions |
|---|---|---|---|---|
| `engie_easy_fixed` | Engie Easy Fixe | EASY, F | fixed | Flanders, Wallonia, Brussels |
| `engie_easy_variable` | Engie Easy Variable | EASY, I | indexed (ZTP101, settled) | Flanders, Wallonia, Brussels |
| `engie_flow` | Engie Flow | FLOW, I | indexed (ZTPDAM) | Flanders, Wallonia, Brussels |
| `engie_direct_online` | Engie Direct Online | DIRECT_ONLINE, I | indexed (ZTPDAM) | Flanders, Wallonia, Brussels |
| `engie_basic_online` | Engie Basic Online | BASIC_ONLINE, I | indexed (ZTPDAM) | Flanders, Wallonia |
| `engie_empower_fixed` | Engie Empower Fixe | EMPOWER, F | fixed | Flanders, Wallonia, Brussels |
| `engie_empower_variable` | Engie Empower Variable | EMPOWER, I | indexed (ZTPDAM) | Flanders, Wallonia, Brussels |
| `engie_empty_house` | Engie Empty House | EMPTYHOUSE, I | indexed (ZTPDAM) | Flanders, Wallonia, Brussels |

Dynamic has no gas card, Basic Online no Brussels one, and the social tariff
is set by the CREG and assigned rather than chosen, so it is not listed.

## How each figure is read

Reader: pypdf (`fetch_pdf_text`). The energy price is the same on the
three regional cards; the DSO table and the levies are not, so the
configured region's card is read.

- Card month: "contrats conclus en Septembre 2026".
- VAT: "Prix, 6 % de tva comprise" gives `card_vat_rate`. Every figure is
  VAT inclusive except the formula.
- Fixed energy: the line after the "Redevance fixe" heading carries the fee
  and the price ("60,00 7,939" on the September 2026 EASY Fixe card).
- Indexed energy: the monthly price and the fee after "Prix mensuels", and
  the formula "Prélèvement: 1,4055 + (0,1020 x ZTP101 (Heren)" in c EUR/kWh
  excluding VAT against an index in EUR/MWh. It is grossed up here by the
  card's rate: `factor = 0,1020 x 1,06 / 100`, `base = 1,4055 x 1,06 / 100`.
  The printed price is the formula at the value the card names.
- DSO table, after "Coûts de réseaux": per tier the fixed term then the
  proportional one, then in Flanders data management, in Brussels the two
  metering fees, then transport. Brussels prints the yearly-read fee and the
  monthly-read one ("SIBELGA 15,90 1,990 43,07 1,447 1001,55 0,808 24,95
  449,24 0,165"); the reader keeps the yearly-read one. Wallonia's five
  ORES rows collapse into one and must agree.
- Excise: "Consommation entre 0 & 12.000 kWh" and "Consommation > 12.000
  kWh".
- Walloon connection fee: "Redevance raccordement(3) 0,00750"; a Walloon
  card without it is refused rather than read as zero.
- Brussels levy: the amounts under "Obligations de Service Public", one per
  meter caliber.

## The index

Two kinds of variable product:

- EASY Variable indexes on ZTP101, the mean of month-ahead quotes taken in
  the month before delivery. It is known when the card is printed and the
  card prices the month on it ("ZTP101 (Heren) du mois = 61,7680 EUR/MWh,
  d'application pour Septembre 2026"), so its price is `settled`.
- FLOW, EMPOWER, DIRECT ONLINE, BASIC ONLINE and Empty House index on
  ZTPDAM, the mean of the delivery month's day-ahead and weekend
  assessments, known only once the month is over. The printed price is the
  formula at the last known value ("Aout 2026: 61,5370 EUR/MWh").

`fetch_index` reads Engie's gas indexation page
(`/fr/energie/electricite-gaz/prix-conditions/parametres-indexation/parametres-indexation-gaz`):
one HTML row per month, newest first, with TTF103, TTF101, ZTP101, ZIGDAM
and ZTPDAM in that order. Quarter rows (older than any product read here),
empty cells and dashes are skipped.

## Archive

`monthOffset` counts months back from the endpoint's current month, so the
same URL is the archive. `fetch_for_month` takes the offset in Home
Assistant's zone (on a UTC host the OS clock is still on the previous month
for the first hours of the 1st in Brussels), refuses a month ahead of
today, and refuses a card naming another month than the one asked for.

## What the tests pin

- EASY Variable: ZTP101, settled, price, fee, factor, base, the formula at
  61,768 reproducing the printed 8,168 c€/kWh, month and VAT.
- FLOW: ZTPDAM, not settled, the formula at 61,537.
- Empty House: its own formula and no fee. EASY Fixe and Empower Fixe.
- Wallonia: the ORES collapse and RESA row in full, the connection fee, the
  August 2026 excise, no energy contribution.
- Flanders: the Kempen row with data management. Brussels: the Sibelga row
  and the nine levy amounts.
- Refusals: another region's card, an unknown contract, a lost formula.
- The index page, contract regions, and `fetch_for_month` (the offset in
  Brussels time, a card for another month, a transient failure, a future
  month).
