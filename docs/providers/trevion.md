# Trevion

Module: `custom_components/be_gas_prices/providers/trevion.py`.
Tests: `tests/test_trevion.py`, fixtures in `tests/fixtures/trevion/`.

## Where the cards are

- Listing: https://trevion.be/tariefkaarten/ (WordPress). It links every
  card Trevion still serves, electricity and gas, residential and
  professional, newest first over pages of seven gas months:
  `https://trevion.be/tariefkaarten/page/<n>/` from page 2 on, and the page
  after the last answers with no card. On 2026-10-19 page 1 held April to
  October 2026, page 2 September 2025 to March 2026, page 4 August 2024 to
  January 2025.
- Gas card: `https://trevion.be/tariefkaarten/Tariefkaart-Gas-Flex-Particulier-<YYYYMM>[-N].pdf`.
  `-N` is a reissue. The unsuffixed name of a reissued month still answers
  (`-202608.pdf` and `-202608-1.pdf` are both 200), so the listing's href
  is used and no URL is ever built. Where a month is listed twice the
  highest `-N` wins.
- The `Professioneel` twin on the same listing is not read.
- No ETag or Last-Modified on the listing, so there is no probe.

## Products and regions

| Contract id | Label | Kind | Regions |
|---|---|---|---|
| `trevion_gas_flex` | Trevion Gas Flex | indexed (monthly TTF_RLP) | Flanders |

The card covers households "aangesloten in Vlaanderen" under 150 MWh a
year, open-ended, online only.

## How each figure is read

Reader: pypdf (`fetch_pdf_text`). pdfplumber lays the header and energy
block out one glyph per line on these Word exports (stray whitespace glyphs
at almost every height); pypdf reads all ten cards from January to
October 2026 cleanly.

- Card month: "Geldig voor particuliere contracten afgesloten in
  `<maand> <jaar>`".
- Product check: "Trevion Gas Flex Particulier", or "Trevion Gas Flex
  (Particulier)" from October 2026, must be on the card.
- Energy: the line after "Energiekost Variabel (c€/kWh) Abonnementskost
  (€/jaar)" carries the price and the yearly fee ("7,20 39").
- Formula: "Tariefformule fossiel gas: (0,102 x TTF_RLP + 0,50) x 1,06",
  c€/kWh against TTF_RLP in EUR/MWh. The "x 1,06" is read and applied; the
  formula is not grossed again. `factor = 0,102 x 1,06 / 100`,
  `base = 0,50 x 1,06 / 100`.
- VAT: "Incl. 6% BTW" gives `card_vat_rate`.
- DSO table, between "Netbeheerder" and "Bijzondere accijns": per tier the
  fixed term then the proportional one, then transport and databeheer:
  `Fluvius Antwerpen 15,68 2,26 83,22 0,91 0,165 18,92`. The shared
  `FLUVIUS_LABELS` match the card's "Fuvius Halle-Vilvoorde" typo.
- Excise: "Bijzondere accijns op Energie (c€/kWh)" then the two band labels
  ("≤ 12 MWh", "≥ 12 MWh") and then the two rates, each on its own line.
- Energy contribution: "Bijdrage op de Energie (c€/kWh) 0" (0,10577 before
  August 2026). Optional: a card that drops the row follows the law.

## The index

Trevion publishes no index table (its marktinformatie page is
electricity only). The card names "the last known value": "De laatst
gekende waarde is deze van augustus 2026 (61,72 €/MWh)". The printed price
is the formula at that value.

The value is always the TTF RLP of the month before the card, as OCTA+
("TTF RLP Mois") and HOA publish it:

| Card | Month named | Value | OCTA+ for the month before |
|---|---|---|---|
| 202601 | december 2025 | 27,65 | 12/2025 27,652 (Bolt's TTF RLP) |
| 202602-1 | januari 2026 | 34,06 | 01/2026 34,067 (Bolt's TTF RLP) |
| 202603 | maart 2026 | 33,30 | 02/2026 33,296 |
| 202604 | maart 2026 | 51,26 | 03/2026 51,264 |
| 202605-1 | april 2026 | 46,00 | 04/2026 46,001 |
| 202606 | mei 2026 | 46,94 | 05/2026 46,941 |
| 202607-1 | juni 2026 | 45,08 | 06/2026 45,076 |
| 202608-1 | juli 2026 | 53.07 | 07/2026 53,075 |
| 202609 | augustus 2026 | 61,72 | 08/2026 61,729 |

The month named on the March card is wrong. `fetch_index` downloads every
card from last year's January card on, January of last year being the
first month the price history can be backfilled from, reading the
listing's later pages for those page 1 no longer holds, and files each
value under the month before the card's own where the card names that
month or its own, as the March card does for February's 33,30; a card
naming any other month is left out. The older cards a page also lists are
not downloaded. `published_index` is the pure reader. One dead link is
skipped, and so is a later page the site no longer serves; a transient
failure raises.

## Archive

The listing is the archive. `fetch_for_month` reads its pages until one
lists the month asked for or an older one, at most six, takes the
listing's href for the month and refuses a card naming another month.
Page 3 lists the August 2025 card as `...-250508.pdf`, which reads as
August 2505, so August 2025 is not served.

## Known quirks and card errors

- "Fuvius Halle-Vilvoorde", and its T2 term printed "0.98" with a dot.
- DSO terms rounded to two decimals, three of them wrongly against the
  Fluvius 2026 tariffs: Kempen T1 2,28 (2,27489), Midden-Vlaanderen T1 2,31
  (2,31716), West T1 2,70 (2,69459). Read as printed.
- The high excise band on the April to June 2026 cards is 0,96229, the
  first-quarter rate; July's 0,98914 is right. The law override bills each
  quarter's rate.
- The May 2026 card prints 5,51 where its formula at the value it names
  (46,00) gives 5,5035; the June card prints 5,60 where 46,94 gives 5,6052.
- The value named is cut rather than rounded at times (61,72 for 61,729).

## What the tests pin

- September card: price, fee, factor, base, the formula check at 61,72,
  month, VAT, the Antwerpen row in full, the Halle-Vilvoorde typo row,
  Kempen as printed, excise and contribution.
- March card: pre-August excise and contribution.
- `published_index` on the September, August and March cards.
- Listing: one href per month, the reissue, no Professioneel card.
- `fetch`, `fetch_index` (the full table, the cards of page 2 back to
  January of last year, the walk reading page 1 alone
  once it reaches the month, a dead link, a later page gone, an outage)
  and `fetch_for_month` (the URL taken from the listing, a month on page 2,
  page 1 alone for a month it lists, a card for another month, a month not
  listed, a transient failure).
- January and February 2026 cards: the values they name.
