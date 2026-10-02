# Frank Energie

Module: `custom_components/be_gas_prices/providers/frank.py`. Tests:
`tests/test_frank.py`, fixtures in `tests/fixtures/frank/`.

## Where the cards are

- The cards are file assets of the Sanity CMS behind the site, found as the
  electricity cards are:
  `https://8navd656.api.sanity.io/v2023-01-01/data/query/production-be` with
  `*[_type=="sanity.fileAsset" && originalFilename match "*Gas*"]`, projected
  to `{originalFilename,url,_createdAt}`.
- The terms page https://www.frankenergie.be/nl/voorwaarden links the five
  current cards ("Actuele Tariefkaart Gas Variabel (ZTP)", "(ZTP) HV", "VT",
  "JN", "Slim"). `fetch` does not read it, but the five cards it picks from
  Sanity are those five links.
- File names: `Frank Energie Tariefkaart Gas[ ZTP][ <tier>] <Maand> <jaar>.pdf`.
  From September 2026 "Gas HV September 2026", before that "Gas ZTP HV
  Augustus 2026" (sometimes with a double space). The standard tier has no
  tier word ("Gas ZTP September 2026"). Runs of spaces are collapsed before
  matching; a name without a month ("Gas ZTP Wintervast 2025", a different,
  fixed product) is ignored.
- `fetch` reads the 40 newest gas uploads, keeps the tier's, takes the newest
  month the names give that is not after the running one (the newest at all
  when there is none) and then the newest upload of it. Frank uploads a
  month's cards in the last days of the month before, which stays priced on
  its own card. Upload time alone would be wrong: the HV and Slim cards of
  March 2026 were uploaded on 1 April, after April's.
- Probe: the `_createdAt` of the newest gas upload and the running month,
  one short query. Any new card changes it, and so does a month beginning
  whose card was uploaded before it.

## Products and regions

| Contract id | Label | Title word | File words | Kind | Regions |
|---|---|---|---|---|---|
| `frank_variable` | Frank Energie Gas Variabel | none | none | indexed, monthly | flanders |
| `frank_variable_hv` | Frank Energie Gas Variabel HV | HV | HV | indexed, monthly | flanders |
| `frank_variable_jn` | Frank Energie Gas Variabel JN | JN, June | JN, June | indexed, monthly | flanders |
| `frank_variable_slim` | Frank Energie Gas Variabel Slim | Slim, SL | SL, Slim | indexed, monthly | flanders |
| `frank_variable_korting` | Frank Energie Gas Variabel Korting | Korting | VT | indexed, monthly | flanders |

- All open-ended ("contracten van onbepaalde duur"); the DSO table is Fluvius
  only. Slim "kan alleen afgesloten worden in combinatie met een 'SL'
  elektriciteitscontract"; JN is "voor klanten met een digitale meter".
- "June" is JN's name up to May 2025: same wording, the same formula
  (0,1010 x ... + 0,24) and the same 35 EUR cashback as the JN cards after it.
  JN's base went from 0,24 to 0,44 in April 2026; the other tiers' formulas
  have not moved since January 2025.
- HV, JN and Korting print a cashback ("Korting 115 EUR (incl. btw)", 35 and
  110 in September 2026). A snapshot has no field for it, so it is not read.

## How each figure is read (pdfplumber layout text)

- Title, the first line, its parts set apart by dashes: "Tariefkaart gas
  variabel contract" ("Tariefkaart gas variabel" from 1 October 2026,
  "Frank Energie Variabel" on the card re-uploaded on 2 October) and the
  month (standard), or "Frank Energie Variabel", the tier and the month. The tier reads "HV ZTP", "JN", "Slim" or "Korting
  ZTP" ("VT ZTP" from October 2026), and "SL" or "Slim ZTP" on older Slim
  cards. The tier word must be the
  contract's and nothing but "ZTP" may follow it, which keeps out the January
  2025 product whose title names both Korting and Slim. The card month comes from the title, not from the sentence
  below it: the Slim cards of January and February 2026 say "getekend in
  december 2026".
- Formula: "Formule Gasprijs: (0,1000 x M ZTP RLP0N EOD EEX + 0,46) x 1,06 in
  EURct/kWh". The index is in EUR/MWh, so factor = 0,1000 / 100 x 1,06 and
  base = 0,46 / 100 x 1,06, with the multiplier the formula prints (not
  grossed again).
- Price: "Verwacht volgens methode VNR voor september 2026 (EURct/kWh)
  7,0818" ("VREG", the regulator's old name, on the older cards). It is the
  formula at a VNR estimate the card does not print; EBEM prints the same
  estimate ("Geschatte ZTP (simulator)", 62,20917 for September 2026) and
  every Frank tier's price comes out of it to the fourth decimal, on every
  card from January 2025 but June 2025 (Frank's prices there imply 37,58,
  EBEM prints 37,74522).
- Fee: "Abonnementskost (EUR/maand) 2,92", times 12.
- VAT: "De tarieven (incl. 6% btw, tenzij anders vermeld)" into
  `card_vat_rate`.
- DSO table, "N ettarieven" (the heading's first letter is set apart) to
  "Informatie over uw tariefformule": Databeheer (EUR/year), Transport
  (EURct/kWh), Kleinverbruik Variabel, Vast, Gemiddeld verbruik Variabel,
  Vast. The text has one figure per line under the area name ("Antwerpen" /
  "18,92" / "0,1654" / ...; Limburg and West share their line with the first
  figure); every line opening on a figure is joined to the one above before
  `read_dsos`. The labels are
  `FLUVIUS_LABELS` without "Fluvius ". No T3.
- Excise: "Bijzondere accijns op Energie (EURct/kWh) kleiner dan 12.000 kWh"
  with its figure on the next line, and "groter dan 12.000 kWh 1,18296".
- Energy contribution: "Bijdrage op Energie (EURct/kWh) 0,1057" up to July
  2026. The row is gone from August 2026, the month the law set the levy to
  zero, so it is required on a card dated before then and 0.0 from then on.

## Index

- M ZTP RLP0N EOD EEX: "de RLP-gewogen ZTP dagprijzen ... (publicatie door
  EEX)", the delivery month's value, known after the month: `settled` False.
- `fetch_index` reads the "Index waarden" table of
  https://www.frankenergie.be/nl/voorwaarden (server-rendered HTML): columns
  Periode, Belpex RLP_BE, M Belpex SPP_BE, M TTF RLP0N EOD EEX, M ZTP RLP0N
  EOD EEX, one row per month from August 2023 ("Augustus 2026 | ... |
  61.9"). The column is found by its heading; dot decimals, the odd comma
  elsewhere in the table; M TTF RLP0N EOD EEX has been empty since June
  2024.
- August 2026: Frank 61,9, EBEM's Argus ZTP-RPL 61,82, Engie's ZTPDAM
  61,537.

## Archive

- `fetch_for_month` asks Sanity for the gas files whose name has the month
  and the year, keeps the tier's, and reads the newest upload first. A file
  named for the tier that holds another tier's card is skipped for the next
  upload of the month: "Gas ZTP Januari 2026 v2.pdf" is the Korting card,
  uploaded after the standard tier's own January card. Any other failure is
  the card's own. No clock is involved; a card naming another month is
  refused.
- Every tier reads from January 2025 to September 2026 except March 2026.
  JN has no upload for January, March and April 2025, Slim none for February
  and May 2025. Uploads go back to July 2023, but the 2024 names carry no
  year ("Gas ZTP December.pdf") and the December 2024 card still lists
  Gaselwest, Intergem, Iveka, Iverlek and Sibelgas.
- Where two uploads of one tier's month differ, the later one is the one the
  months around it agree with: the first "June Februari 2025" prints the
  standard formula and a 2,08 fee, the first "JN April 2026" base 0,24 where
  May onward prints 0,44, and the first "SL Oktober 2025" HV's expected
  price (3,6771) beside the standard formula.

## Known quirks and card errors

- Every March 2026 card lost the last character of every text line ("maart
  202", "Abonnementskost (EUR/maand 2,9" for 2,92, "in EURct/kW"). The glyphs
  are not in the PDF. The title's short year refuses the card, and the
  formula's cut unit would too.
- The January and February 2026 Slim cards date their formula "getekend in
  december 2026"; the title and the VNR line give the right month.
- "Frank Energie Tariefkaart Elektriciteit Gas ZTP SL Januari 2026.pdf" is
  the gas Slim card of January 2026, despite its name.
- West T2 proportional prints 1,06 where energie.be prints 1,05 (Fluvius
  1,05500), a rounding difference.

## Tests

- `test_each_tier_reads_its_formula`: formula, expected price, fee and the
  check at EBEM's 62,20917, per tier.
- `test_a_tier_refuses_every_other_tiers_card`: all 25 pairs.
- `test_dso_table_rebuilt_from_one_figure_per_line`: Antwerpen in full,
  Kempen, West.
- `test_levies_as_printed`, `test_july_card_still_prints_the_contribution`,
  `test_a_card_before_august_without_the_contribution_is_refused`.
- `test_slim_title_as_sl_and_the_title_wins_over_the_sentence`,
  `test_the_clipped_march_2026_cards_are_refused`, `test_only_flanders`.
- `test_index_page`.
- `fetch` (newest month of the tier), `fetch_for_month` (the misnamed
  January upload skipped, the query's month and year, month refusal, the
  clipped card, transient raise), `probe` and `fetch_index`, with the fetch
  helpers mocked.
