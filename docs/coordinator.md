# The coordinator

`GasCoordinator` (`coordinator.py`) is a `DataUpdateCoordinator` ticking
hourly (`UPDATE_INTERVAL_MINUTES`). This page follows one tick and what the
coordinator keeps between them.

## One tick

`_async_update_data` runs `_tick` and, success or failure, syncs the Repairs
cards (`issues.sync_issues`).

1. **The card** (`_refresh_snapshot`). It is fetched when there is none, when
   the refresh button or service forced it, when the card in hand came from
   the Store or the card archive, when the supplier's probe key changed, or,
   without a probe, when it is 24 hours old. It is read through
   `month_cards.current_card`: a card published as page images, which no
   reader here reads, is priced on the card archive's row for the running
   month, which the archive read with its OCR engine (`card_source` `ocr`,
   the `card_read_by_ocr` Repairs card); that reading is asked for again on
   the same schedule. A failure keeps the card in
   hand and records `last_error`; a failure a retry will not fix counts
   towards the `extractor_failed` card. With no card at all, or a stale one,
   the card archive is asked for its latest row (`_adopt_archived_card`).
   With still no card the tick raises `UpdateFailed`, which is what makes a
   first setup retry. The expert custom supplier builds its card from the
   entry instead.
2. **Index values** (`_refresh_index`), twice a day, from the supplier's own
   publication. A failure keeps the table held.
3. **Calorific values** (`_refresh_calorific`), daily, for a household that
   converts on its reception station: every month Atrias lists since
   January of last year, only the months not held yet.
4. **Past months' cards** (`_fill_month_cards`) for every closed month of the
   window and the signing month. The first tick does this in the background,
   because it is what setup waits on, and requests a refresh when done.
5. **The meter** (`_read_meter`): the configured sensor, else the first gas
   source of the Energy dashboard, read as daily changes from 1 January of
   last year.
6. **The bill** (`_build`): the measured rolling year replaces the typed
   volume where there is one, the current contract's months are walked from
   its first day this year, earlier contracts are billed on their own cards,
   and the current price is the current month billed at zero kWh. A past
   month whose own card cannot price the household is billed on the current
   card; when the current card cannot either (its DSO row or the tier is
   missing), the tick records `last_error` and raises `UpdateFailed`.
7. **The daily ranking** (`_maybe_rank`), when the entry asks for it, once a
   day at a minute derived from the entry id, in the background.
8. The Store is written.

## What is kept, and where

The entry's Store (`.storage/be_gas_prices.<entry_id>`) holds the last card
and when it was fetched, the index table, the calorific values of the
configured station, the past months' cards, the price-history stamp and the
last daily ranking. A blob written under another snapshot schema is dropped,
not migrated: everything in it is re-derivable.

## Staleness

`snapshot_stale()` is true once the card is a week old (a week without a
successful fetch, or a probe answering the key of the card in hand, which
counts as one) or a week past the last day it prices (a supplier that has
not published a new month). It raises the `snapshot_stale` Repairs card,
whose fix flow fetches again, and it is what lets the card archive stand in.

## Services and the button

- `be_gas_prices.refresh` and the *Refresh tariff card* button force the next
  tick to fetch the card and the index values whatever their age.
- `be_gas_prices.backfill_statistics` rewrites the price sensors' hourly
  statistics from a start date (default: the start of the year's window).
  `clear: true` deletes those statistics in full first, since the recorder
  has no windowed delete, and requires an `entry_id`.

## The automatic price history

`backfill.backfill_once_a_year` runs after setup and after each update, and
does nothing unless what the year is drawn from has moved: the calendar year,
which past months have their own card, the latest index value and calorific
value, and the current card's month. It is stamped in the Store, and skipped
where Home Assistant runs no recorder.
