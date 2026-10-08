# Bolt

Module: `custom_components/be_gas_prices/providers/bolt.py`. Tests:
`tests/test_bolt.py`, fixtures in `tests/fixtures/bolt/`.

## Where the cards are

- Fixed cards: `https://files.boltenergie.be/pricelists/fix/<fix|plenty_fix>_res_ng_fr_<YYYYMM>.pdf`,
  the electricity pattern with `_ng_` for `_el_`.
- Variable cards: `https://files.boltenergie.be/pricelists/var/<bolt|online|plenty|plenty_online>_res_ng_fr_<n>.pdf`,
  `n` a version number. One card covers Flanders, Wallonia and Brussels.
- `fetch` reads the listing https://www.boltenergie.be/fr/listes-des-prix,
  which links each current card (`pricelists/fix/fix_res_ng_fr_202609.pdf`,
  `pricelists/var/online_res_ng_fr_14.pdf`), and takes the latest month or
  version linked for the product. The listing also links the Dutch and
  professional cards, which the pattern
  `pricelists/<fix|var>/<slug>_res_ng_fr_<digits>.pdf` leaves out.
- Probe: the listing gives the card URL, then a HEAD on the card gives its
  Last-Modified. The card says "Le prix proposé peut évoluer dans le courant
  du mois", and the September 2026 card was last modified on 14 September
  ("Mon, 14 Sep 2026 12:07:39 GMT"), so the card's own header is the signal,
  not the listing's ETag.
- Reader: pdfplumber layout (`fetch_pdf_text_layout`, 60 s timeout for a
  1,7 MB card). About 40 to 45 s per card on a Raspberry Pi, most of it
  loading the vector art of each page; page 3, which nothing is read from,
  takes about half. pypdf is fast but scatters the table column by column
  and splits figures ("1 8 , 92").

## Products and regions

| Contract id | Label | Slug | Card title | Kind | September 2026 |
|---|---|---|---|---|---|
| `bolt_fix` | Bolt Fixe | fix | Bolt Fixe | fixed | 8,02 c€/kWh, 8,99 €/month |
| `bolt_plenty_fix` | Bolt Plenty Fixe | plenty_fix | Plenty Fixe | fixed | 8,02 c€/kWh, 3,99 €/month |

| Contract id | Label | Slug | Card title | Kind | October 2026 |
|---|---|---|---|---|---|
| `bolt_variable` | Bolt Variable | bolt | Bolt Variable | indexed (TTF) | 8,34 c€/kWh, 8,99 €/month |
| `bolt_variable_online` | Bolt Variable Online | online | Bolt Variable Online | indexed (TTF) | 8,34 c€/kWh, 5,99 €/month |
| `bolt_plenty_variable` | Bolt Plenty Variable | plenty | Plenty Variable | indexed (TTF) | 8,34 c€/kWh, 3,99 €/month |
| `bolt_plenty_variable_online` | Bolt Plenty Variable Online | plenty_online | Plenty Variable Online | indexed (TTF) | 8,34 c€/kWh, 0,99 €/month |

- Regions: flanders, wallonia and brussels for all six.
- The two fixed cards print the same price and tables. Plenty Fixe charges
  the lower platform fee and "vous devez détenir au moins une part de la
  coopérative de 250 euros auprès de Plenty Coop SC".
- Fixed for a year from the start of the contract; the price applies to
  contracts concluded in the card's month.
- The four variable cards print the same formula, price and tables and
  differ by their fee. The Dutch editions of the October 2026 cards print the
  same price, fee and formula as the French ones.
- A variable contract has no end date ("De leveringsovereenkomst is van
  onbepaalde duur") and keeps the formula of the card it was signed on: the
  general terms of 14/02/2025 put the price in the contract's particular
  conditions (art. 8.1) and let Bolt change it only with two months'
  individual notice (art. 13.1 and 13.2).

## How each figure is read

- The text first goes through `_join_value_lines`: pdfplumber sets each table
  value on its own line after its label ("Fluvius Imewo\n 2,628\n 18,61 ..."),
  with blank lines between rows. Every line holding only figures and dashes
  is appended to the line above, and U+2028 is taken as a line break. On the
  current cards the only other line this joins is the phone number under the
  e-mail address.
- Title: "Carte Tarifaire / Bolt Fixe / Septembre 2026 /Résidentiel /Gaz". The
  product must be the contract's card title, or the parse fails. The month
  gives `publication_label`, and on a fixed card `valid_until`. A variable
  card is in force until the next version, so its `valid_until` is None. The
  August 2026 card spells it "Aôut", which folds to "aout".
- Energy: "Prix mensuel 8,02" (c€/kWh TTC) and "€ 8,99 / mois", times 12 for
  `yearly_fixed_fee`.
- VAT: "TTC" everywhere, no rate stated, so `card_vat_rate` is None. The only
  formula the fixed card prints is "Simple Fixe".
- Variable formula, under "Type de compteur TTF Q3 2026 Formule tarifaire
  (€/MWh, HTVA)": "Simple 65,38 €/MWh TTF *1,049 + 10,10", the TTF value the
  printed price is set at, then the formula. Factor and base are divided by
  1000 for EUR/kWh and grossed by the residential 6%, which reproduces the
  printed price: (65,38 x 1,049 + 10,10) / 10 x 1,06 = 8,34. The index is
  filed as "TTF". `price` is the printed "Prix mensuel".
- DSO table, "Distribution et transport TTC", read between that heading and
  "Taxes et redevances": columns Petite consommation <= 5.000 kWh (Variable
  c€/kWh, Fixe €/an), Consommation moyenne > 5.000 et <= 150.000 kWh (the
  same), Transport (c€/kWh), Relevé de compteur (€/an). Proportional before
  fixed within each tier. No T3.
  - Flanders: "Fluvius Antwerpen 2,257 15,68 0,905 83,22 0,1654 18,92". West
    is labelled "Fluvius-West"; the shared labels match it.
  - Rows "Fluvius (Gaselwest)", "(Iveka)", "(Iverlek)" and "(Sibelgas)"
    print "- - - - 0,1654 -": leftovers from before the 2025 merger, ignored.
  - Wallonia: five ORES rows (collapsed, must agree) and "TECTEO RESA".
    Metering "-".
  - Brussels: "SIBELGA 1,990 15,90 1,447 43,07 0,165 24,95". Transport is
    0,165 here and 0,1654 on the other rows.
- Levies, "Taxes et redevances TTC", three columns Flandres, Wallonie,
  Bruxelles, some rows with a footnote digit after the label:
  - "Contribution sur l'énergie (c€/kWh) 2 - - -": a dash is no
    contribution. The row itself is required.
  - "Accise fédérale (c€/kWh) 1,09286 1,09286 1,09286": one rate, kept as a
    single open band.
  - "Redevance de raccordement (c€/kWh) 3 - 0,00750 -": mandatory for
    Wallonia.
  - Each value must be a figure with decimals or a dash. A row that lost a
    value does not match, so the footnote digit cannot slide into the
    Flanders column.
- Brussels levy, "Obligations de service publique (Bruxelles) €/an": "6 of 10
  m3/h 4 3,56", "6 of 10 m3/h 5 12,54", then 16, 25, 40, 65, 100 and 160
  m3/h. Eight amounts, passed to `osp_table` in row order.

## The index

- The variable cards bill each day at that day's TTF day-ahead value ("Dans
  la facturation, la consommation journalière est multipliée par la valeur
  de la TTF pour cette journée"). A consumption the network operator reports
  without daily values is spread over the days by the Synergrid RLP, which
  makes the month's bill the RLP-weighted mean of the month.
- The listing carries that mean as `"priceHistory":{"electricity":[...],
  "gas":[{"date":"2026-08-31T22:00:00+00:00","price":0.0753495763449174},
  ...]}`: one row per month, dated the first of the month at midnight in
  Belgium, priced in EUR/kWh, under no name. `fetch_index` reads it with the
  JSON decoder from that key, converts to EUR/MWh and keeps the months before
  the current one (Home Assistant's clock). The dates are read in
  Europe/Brussels whatever Home Assistant's zone: in UTC or Europe/London
  every value would otherwise be filed a month early, September taking
  October's forward value. A date without its offset is refused.
- It matches OCTA+'s "TTF RLP Mois" (EGSI TTF day-ahead weighted by the RLP,
  https://files.octaplus.be/tariffs/paramètres_gaz_fr.pdf) to 0,03 EUR/MWh
  every month from September 2025 to September 2026, and to 0,17 from May to
  August 2025.
- The running month and the three after it hold forward values, and the
  running month's changes until the month is over: on 29 September 2026 it
  held 76,01 for September, which read 75,35 on 7 October.
- A household whose daily values reach Bolt is billed on its own daily
  profile, which the monthly mean only approximates.
- The card's own source for the daily values is
  https://www.powernext.com/spot-market-data, now EEX. Nothing here reads it.

## Card errors and differences, read as printed

- ORES proportional terms 4,198 (T1) and 2,115 (T2) c€/kWh where the 2026
  grid (CWaPE summary of 4 November 2025) and the other cards say 4,289 and
  2,206. The fixed terms 31,91 and 140,93 agree. The 2026 grid with the
  regulatory balance term of decision CD-24k29-CWaPE-1008 (0,0011072 EUR/kWh)
  in place of the one decision CD-25j16-CWaPE-1146 of 16 October 2025 set
  (0,0019706) gives 4,198 for T1 exactly and 2,114 for T2. Decision 1008's
  text rounds the T2 distribution term, so the last digit is not checkable.
- RESA T2 proportional 2,259 where the grid says 2,52946 and every other card
  but Ecofix's 2,529: most likely two digits swapped. RESA T1 4,640 and the fixed terms
  agree.
- A single excise rate: 1,09286 is the law's first slice (10,31 EUR/MWh plus
  6%); the second slice (1,18296) is not printed.
- The Brussels levy prints 12,54 for a small meter above 5 000 kWh where the
  Engie and Mega September cards print 12,59, and has no row above 160 m3/h
  (970,41 on those two cards).
- The April, June, July and August 2026 cards print the energy contribution
  0,1058 in every region and the excise 0,8724; the August card is therefore
  stale for August deliveries. `_resolve` applies the delivery month's law.

## The archive

- Past fixed cards stay served under their month. On 2026-09-29 `fix`
  answered for 202501, 202506, 202512, 202601, 202608 and 202609, and
  `plenty_fix` for the same months except 202501 (404). The next month
  (202610) is 404 for both.
- `fetch_for_month` builds the URL from the month asked for; 404 is no card, a
  transient failure raises, and a card naming another month is refused.
- Template: the current three-page card starts with April 2026. The January
  and March 2026 cards (the ones checked before April) are an older two-page
  layout ("Coût de l'énergie Simple c€4,57 /kWh", "Abonnement €10,99/mois")
  that this module does not read, so such months return None and the month
  cache falls back to the card archive.
- Seen on the current template: 10,99 €/month for Bolt Fixe on the April and
  June cards, 8,99 from July; prices 6,95 (April), 6,21 (June), 5,76 (July),
  7,31 (August), 8,02 (September).
- Variable cards are addressed by version, and the old versions stay served. On
  2026-10-07 the French ones were:

  | Version | Title | Formula (EUR/MWh) | Read here |
  |---|---|---|---|
  | 10 | Janvier 2025 | TTF * 1,0302 + 9,013 | no, the older two-page template |
  | 11 | Juin 2026 | TTF * 1,0302 + 9,013 | yes |
  | 12 | Juin 2026, Bolt Variable and Variable Online only | TTF * 1,09 + 10,90 | no, the Flanders rows and levy column are missing from the text |
  | 13 | Septembre 2026 | TTF *1,049 + 10,10 | yes |
  | 14 | Octobre 2026 | TTF *1,049 + 10,10 | yes |

  Plenty and Plenty Online have no version 12 (404). No card is titled July
  or August 2026.
- `fetch_for_month` for a variable contract takes the newest version whose
  title names the month or an earlier one, which is the card in force at the
  end of that month. It walks down from the version the listing links,
  reading titles with pypdf (about 9 s a card on a Raspberry Pi, against 40
  to 45 s for the layout reader), passes over a number that fails to download, and stops at a title
  it does not read. A transient failure raises. Only the chosen card is read
  with the layout reader; when it does not parse, the month has no card. The
  month cache keeps a closed month's card, so the walk runs once for a month
  that has one. A month with none is asked again the next day, and one fill
  of the month cache reads each version once for all its months.
- The card archive's backfill files such a card under the month its title
  names, so asking July 2026 for Plenty Variable gives a June row. A replay
  walks again on the texts the backfill recorded; a number that answered 404
  left none, so a Plenty row whose walk passed version 12 is kept as it was
  and reported as not replayable.
- Signing months are whole months: a contract signed in June 2026 before the
  22nd was on version 11, but the walk gives version 12, which is not read,
  so the month falls back like any other month without a card. The household
  can type its factor and base instead.

## Tests

`tests/test_bolt.py` (180 s per-test timeout: the first test to read a card
pays pdfplumber's 45 s on a Raspberry Pi):

- Bolt Fixe price and fee, month, VAT basis; Plenty Fixe fee; the Bolt Fixe
  card refused for Plenty Fixe.
- Flanders table (Antwerpen in full, West not read off the Gaselwest row),
  Wallonia table as printed (ORES and RESA in full), Brussels row and levy
  table.
- Walloon levies; the August card ("Aôut", old excise and contribution); a
  levy row missing a value fails.
- The six contracts registered, in all regions, with an index.
- `fetch` and `probe` take the card the listing links, fixed and variable;
  `fetch_for_month` URL, another month refused, 404 as None, transient
  failure raised.
- Variable cards: the formula against the printed price, the four fees, the
  tables in each region, another product refused, no `valid_until`.
- The version walk: September takes version 13; July takes the June card,
  passing over a missing number, and gets no card since version 12 is not
  read; a title not read stops the walk; a transient failure raises.
- The index: the October listing's months up to September, September's
  forward value left out on the 29 September listing, a listing without the
  series refused, `fetch_index` on Home Assistant's clock, the months read in
  Belgium with Home Assistant set to UTC, a date without its offset
  refused.
