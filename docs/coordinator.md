# The coordinator

`GasCoordinator` (`coordinator.py`) is a `DataUpdateCoordinator` ticking
hourly (`UPDATE_INTERVAL_MINUTES`). This page follows one tick and what the
coordinator keeps between them.

## One tick

`_async_update_data` runs `_tick` and, success or failure, syncs the Repairs
cards (`issues.sync_issues`).

1. **The card** (`_refresh_snapshot`). It is fetched when there is none, when
   the refresh button or service forced it, when the card in hand came from
   the Store or stands in from the card archive, when the supplier's probe
   key changed, without a probe when it is 24 hours old, and, probe or not,
   when its month is over. The probe is asked before every fetch, so the key
   a card was fetched under is kept.
   It is read through `month_cards.current_card`: a card published as page
   images, which no reader here reads, is priced on the card archive's row
   for the running month, which the archive read with its OCR engine
   (`card_source` `ocr`, the `card_read_by_ocr` Repairs card); that reading
   is asked for again on the same schedule and once a day besides, since the
   archive reads a card reissued in the month after its key moved. Until the
   archive reads a new month's card, its reading of an earlier one, held,
   restored from the Store or standing in, is a wait rather than an
   unreadable card. A card put up before its month began gives way to the
   running month's own card from the supplier's archive, where it has one;
   an archive that fails to answer fails the fetch, asked again next tick.
   The fetch has 180 s (`CARD_FETCH_BUDGET_S`, twice Bolt's slowest path
   measured on a Raspberry Pi 4); one that runs past it is given up like a
   network failure.
   A failure keeps the card in hand and records `last_error`; a failure a
   retry will not fix counts towards the `extractor_failed` card, or the
   `card_missing` card when it found no card at the address. An error
   no extractor raises on purpose (a parser meeting a page it does not
   expect) is failed the same way, through `providers._pdf.guarded`, with
   its traceback logged; a probe raising one is no signal. With no
   card at all, or a stale one, the card archive is asked for its latest row
   (`_adopt_archived_card`). With still no card the tick writes the Store,
   so the next setup retry counts the failures on, and raises
   `UpdateFailed`, which is what makes a first setup retry. The expert
   custom supplier builds its card from the entry instead, and a contract
   the supplier withdrew fetches nothing: the card held, or when none is
   the card of the last month it was sold (`last_month_sold`), from the
   card archive where the entry reads it, else from the supplier's own
   archive, prices it and never goes stale.
2. **Index values** (`_refresh_index`), twice a day, from the supplier's own
   publication. A failure keeps the table held, or with none held and a
   failure a retry may cure (network, storage, HTTP 5xx, 403, 408, 429) is
   asked again on the next tick. A value held for a closed month stays
   when the publication no longer gives it (`bill.merged_index`), as a
   value printed on a card does once the card leaves the supplier's
   listing; the running month's and later ones are the publication's own.
3. **Calorific values** (`_refresh_calorific`), daily, for a household that
   converts on its reception station: every month Atrias lists since
   January of last year, only the months not held yet. Atrias's key or month
   list failing is asked again on the next tick; a month file failing is
   too, while nothing is held for the station and a retry may cure it.
   Then **the excise law** (`excise_law.ensure_excise_law`), daily for the
   whole process, from Justel, and **FPS Finance's excise tariff**
   (`tarbel.ensure_tariff`), the quarters the notices set, the same way: a
   failure keeps what is held (read before, or restored from the Store) and
   is asked again six hours later. An
   entry billing a Flemish contract this year, the current one or one it
   left before moving, then reads **Fluvius's gas tariff sheet**
   (`fluvius.ensure_tariff_sheet`) of this year, once for the process,
   with the same six hour wait after a failure.
4. **Past months' cards** (`async_fill_month_cards`) for every closed month
   the current contract supplied this year and the signing month (and the
   signing day, for a supplier with `fetch_on_day`), and
   (`PeriodBilling.fill`) the earlier contracts' own, each from the card
   archive, else the supplier's archive (`MonthCardCache.card`). The first
   tick does this in the background, because it is what setup waits on, and
   prices again on them when done (`async_reprice`), without asking the
   supplier or Atrias again.
5. **The meter** (`_read_meter`): the configured sensor, else the first gas
   source of the Energy dashboard, read as daily changes from today's date
   last year (`running_costs.meter_start`), which covers this year, the
   rolling year and the year-end projection; nothing before is read.
   Setup's own tick reads no meter: Home Assistant waits on it, with 300 s
   for every integration together, and a year of statistics can take
   seconds on a database on a NAS. That tick publishes the figures the last
   tick that read the meter left (`_held_for`): the measured volume, and
   the year's and month's costs and volumes while they still cover this
   year and month, were priced under the entry's settings and were read off
   the meter in use. Setup then starts the tick that reads the meter in the
   background (`async_reprice`, `meter_reads_pending`), which asks neither
   the supplier nor Atrias again, seconds after its own tick did, unless a
   fetch was forced meanwhile. It and the past months' reprice each run
   only while their work is still to do, so whichever runs first reads the
   meter for both: a restart whose past months' cards are all in the store
   reads it once, and cards fetched again land later and are priced by a
   second read. Where the card cannot price the volume setup's tick has,
   held or typed, setup's tick reads the meter after all, since the
   measured volume may fall in a tier the card does price. A tick that
   finds the energy manager still loading, where discovery gives no count,
   reads no meter either: it publishes the held figures as setup's tick
   does, for the meter they were read off, keeps them in the Store and
   leaves the meter card alone. It does not leave `meter_reads_pending`
   set, since a load that failed never ends: the ranking and the price
   history go ahead on the held volume, or the typed one when none is held,
   and the next tick that names the meter reads it. A read the recorder
   fails (`meter_error`, the `meter_unit` card) is held the same way: the
   last figures stand for that tick, priced on the held volume, rather than
   unknown, and the Store keeps the figures of the last read that answered.
6. **The bill** (`_build`): the measured rolling year replaces the typed
   volume where there is one, the current contract's months are walked from
   its first day this year, earlier contracts are billed on their own cards,
   and the current price is the current month billed at zero kWh. A past
   month whose own card cannot price the household is billed on the current
   card; when the current card cannot either (its DSO row or the tier is
   missing), the tick records `last_error`, writes the Store, so a setup
   retry does not count on a failure count the fetch reset, and raises
   `UpdateFailed`.
7. **The daily ranking** (`maybe_rank`), when the entry asks for it, once a
   day at a minute derived from the entry id, in the background, never
   before the meter read setup starts after its own tick has landed. A time
   listener starts it at that minute; a tick after it catches a day the
   minute was missed on. A stored ranking made for another contract, or
   under other settings, is not restored, so a change of contract or of the
   household's settings ranks the day again. The household's own row is
   quoted on the card in hand with its signing and typed figures, the same
   way as every other row. A sweep that priced no other contract is not
   kept, so the next tick tries again.
8. The Store is written.

## What is kept, and where

The entry's Store (`.storage/be_gas_prices.<entry_id>`) holds the last card,
when it was fetched, how many fetches of it failed in a row and whether it
was found unreadable or missing, the index table, the calorific values of the
configured station, the past months' cards, the price-history stamp, the last
daily ranking and the figures the last meter read gave, with the meter,
settings, year and month they were read and priced for. The failure count
and the unreadable and missing marks, kept for the supplier, contract and
region the card is fetched for, carry the `extractor_failed`,
`card_missing` and `card_unreadable` Repairs cards across a restart, so one the user ignored stays ignored, and
across setup retries with no card, so `extractor_failed` is raised there
too. A blob of an older snapshot schema, down to `SNAPSHOT_SCHEMA_FLOOR`,
is read as it is, so a bump that adds a field keeps the stored cards and the
archive's rows not yet rewritten: the card is asked for again at once and
kept only while no fetch succeeds. A blob of a newer schema or below the
floor is dropped. The past months' cards another release of the integration
stored are read again, even across a restart, so a release that reads a card
better reaches them, and kept when no card of the month can be found any
more. A release that refuses a card an older one misread raises the floor to
its schema, which drops them all.

## Staleness

`snapshot_stale()` is true once the card is a week old (a week without a
successful fetch, or a probe answering the key of the card in hand or the
archive's OCR reading taken as a stand-in, which count as one; any other
stand-in is as old as its month) or a week past the last day it prices (a supplier that has
not published a new month). It raises the `snapshot_stale` Repairs card,
whose fix flow fetches again at once and keeps the card when the card in
hand is still stale, and it is what lets the card archive stand in.

## Services and the button

- `be_gas_prices.refresh` and the *Refresh tariff card* button fetch the card,
  the index values and the calorific values whatever their age, in a tick
  that waits for the one running to end rather than a requested refresh,
  which Home Assistant drops when a long tick holds the lock at the end of
  its cooldown. A full tick that checks its card after the press, past the
  supplier's probe, makes the fetch for it, so one press fetches once; a
  card already being fetched when the press comes is the one it gets, unless
  that fetch fails, when the press makes one of its own. Index or calorific
  values already being fetched are the ones it gets; if that fetch fails,
  they are retried when any failed fetch of theirs would be. The service
  returns once the fetch is done, so what follows it reads the prices it
  gave; the button does not wait.
- `be_gas_prices.backfill_statistics` rewrites the price sensors' hourly
  statistics from a start date (default: the start of the year's window) to
  the last full hour, or the one before it in the first minutes of an hour,
  while the recorder compiles that one itself, never before the last recorded
  change of contract, whatever its year: the hours before it were the
  earlier contract's. A month before the year's window is priced on
  its own card only, fetched for the occasion, and skipped where none is
  found. A start date before 1 January of last year is refused, the index
  values read off cards (Trevion, Belvus) reaching no further back, and so
  is one in the future, which a clear would otherwise delete the history
  for.
  `clear: true` deletes those statistics in full first, since the recorder
  has no windowed delete, and requires an `entry_id`. The service also
  rewrites the year's cost over the year's window, whatever the start date,
  and a clear leaves it alone: deleting it would take the years before with
  it.

## The automatic price history

`backfill.backfill_once_a_year` runs after each update once a tick goes to
read the meter (never while setup's own tick, which reads none, is all there
is; one still waiting for the energy manager draws on the held volume, or the
typed one when none is held), from the start of the year's window
(`window_start`: 1 January, or the contract start when the entry counts from
it, or that of an earlier contract of the year that counted from it, so a
switch recorded or removed leaves the year's `last_reset` where it was), and
does nothing unless what the year is drawn from has moved: the calendar year,
the entry's settings, each past month's own card and the current card as read
(so a card read again after an update redraws it), and the index values and
calorific values (so the value of an index that lags the supplier's others
redraws it when it lands or is revised), the figures of the law (so a release,
a Justel or FPS Finance read or a Fluvius sheet that gives another month's
redraws it), and the tier the measured volume falls in (so a year that crosses
into another tier redraws its hours on it; the excise blend within a tier, a
few hundredths of a cent, does not). It is stamped in the Store, and skipped
where Home Assistant runs no recorder.

## The automatic cost history

Home Assistant counts a new `TOTAL` sensor's statistics from its first value,
so without this a monthly graph of `current_year_cost` on a fresh entry starts
at the install. `backfill_cost.backfill_cost_once` runs after the price
history and writes the sensor's hourly rows from the start of the year's
window to the last full hour, on the bills of the last tick: each bill's gas
spread over its days' hours as the meter's hourly changes rose (a day the
meter fell on weighs what its daily change, the one the bill counted, gives),
each day's share of the fixed costs at its local midnight, as the sensor
counts it. The last row is then the sensor's figure. It waits for a tick that
read the meter and knows the cost (setup's own tick holds the figure without
its bills), and for the sensor to be registered.

The rows are joined to what the recorder compiled from the live sensor: they
continue the total recorded before the window (last year's whole bill, on an
entry that ran through it, since the compile carries its total on across a
new cycle), and the rows compiled after them, short-term and hourly, are moved
onto the imported total, with a short-term row at the last imported hour that
the compile resumes from. When the recorder does not answer one of those reads
nothing is written. It is stamped in the Store on what the price history is
stamped on and the meter, so it runs again when either moves.
