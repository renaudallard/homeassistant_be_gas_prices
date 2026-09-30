# Bolt

Module: `custom_components/be_gas_prices/providers/bolt.py`. Tests:
`tests/test_bolt.py`, fixtures in `tests/fixtures/bolt/`.

## Scope: fixed products only

Bolt sells four variable gas products (Variable, Variable Online, Plenty
Variable, Plenty Variable Online) and two fixed ones. Only the fixed ones are
supported. The variable cards say "Dans la facturation, la consommation
journalière est multipliée par la valeur de la TTF pour cette journée": each
day is billed at that day's TTF day-ahead value, and the only daily source of
it forbids reuse. The maintainer decided not to support them, so they are not
registered, and the module never contacts EEX.

## Where the cards are

- Card: `https://files.boltenergie.be/pricelists/fix/<fix|plenty_fix>_res_ng_fr_<YYYYMM>.pdf`,
  the electricity pattern with `_ng_` for `_el_`. One card covers Flanders,
  Wallonia and Brussels.
- `fetch` reads the listing https://www.boltenergie.be/fr/listes-des-prix,
  which links each current card (`pricelists/fix/fix_res_ng_fr_202609.pdf`),
  and takes the latest month linked for the product. The listing also links
  the Dutch, professional and variable cards, which the pattern
  `pricelists/fix/<slug>_res_ng_fr_<6 digits>.pdf` leaves out.
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

- Regions: flanders, wallonia and brussels for both.
- The two cards print the same price and tables. Plenty Fixe charges the
  lower platform fee and "vous devez détenir au moins une part de la
  coopérative de 250 euros auprès de Plenty Coop SC".
- Fixed for a year from the start of the contract; the price applies to
  contracts concluded in the card's month.

## How each figure is read

- The text first goes through `_join_value_lines`: pdfplumber sets each table
  value on its own line after its label ("Fluvius Imewo\n 2,628\n 18,61 ..."),
  with blank lines between rows. Every line holding only figures and dashes
  is appended to the line above, and U+2028 is taken as a line break. On the
  current cards the only other line this joins is the phone number under the
  e-mail address.
- Title: "Carte Tarifaire / Bolt Fixe / Septembre 2026 /Résidentiel /Gaz". The
  product must be the contract's card title, or the parse fails. The month
  gives `publication_label` and `valid_until`. The August 2026 card spells
  it "Aôut", which folds to "aout".
- Energy: "Prix mensuel 8,02" (c€/kWh TTC) and "€ 8,99 / mois", times 12 for
  `yearly_fixed_fee`.
- VAT: "TTC" everywhere, no rate stated, so `card_vat_rate` is None. The only
  formula the fixed card prints is "Simple Fixe".
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

## Card errors and differences, read as printed

- ORES proportional terms 4,198 (T1) and 2,115 (T2) c€/kWh where the 2026
  grid (CWaPE summary of 4 November 2025) and the other cards say 4,289 and
  2,206. The fixed terms 31,91 and 140,93 agree. The 2026 grid with the
  regulatory balance term of decision CD-24k29-CWaPE-1008 (0,0011072 EUR/kWh)
  in place of the one decision CD-25j16-CWaPE-1146 of 16 October 2025 set
  (0,0019706) gives 4,198 for T1 exactly and 2,114 for T2. Decision 1008's
  text rounds the T2 distribution term, so the last digit is not checkable.
- RESA T2 proportional 2,259 where the grid says 2,52946 and every other card
  2,529: most likely two digits swapped. RESA T1 4,640 and the fixed terms
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
- Only the two fixed contracts are registered, in all regions, with no index.
- `fetch` and `probe` take the card the listing links; `fetch_for_month`
  URL, another month refused, 404 as None, transient failure raised.
