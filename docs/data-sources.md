# Data sources

Everything the integration reads besides the suppliers' cards.

## Index values

Each supplier with indexed products publishes the values its bills settle
on, and `SupplierExtractor.fetch_index` reads that publication: Engie's
indexation page, OCTA+'s parameters PDF, TotalEnergies's history PDF, Eneco's
indexation PDF, Mega's indexation page, Luminus's indexation PDF, the series
on Bolt's price list page, and so on
(see each [provider page](providers/)). The table is fetched twice a day and
kept in the entry's Store. A supplier's values are never used for another
supplier's formula: publishers of "the same" hub disagree in the second
decimal (ICIS Heren, EEX and Argus assess it independently) and an RLP
weighting moves it again.

## Calorific values (Atrias)

`calorific.py` reads the monthly gross calorific value of every gas reception
station from Atrias:

- the public API key comes from `https://www.atrias.be/runtime-config.js`;
- `GET https://api.atrias.be/roots/folder/list?folder=SectorData/02 Gross Calorific Values`
  lists the published months, one file per month, published in the first
  days of the next month;
- `GET https://api.atrias.be/roots/download/<path>` returns the month's CSV
  (station name, EAN, value in kWh/m³(n)), whose format varies from file to
  file and is read tolerantly, in a worker thread.

Each answer is held to 256 KiB as it streams: on 6 November 2026 the folder
list was 9 kB, a month's file 4 kB and the runtime config 668 bytes.

`api.atrias.be` does not send its intermediate certificate. GoDaddy's public
intermediates ship in `certs/godaddy_intermediates.pem` and are added to an
otherwise standard client context; verification is never turned off. The file
holds "Go Daddy Secure Certificate Authority - G2", which issued the
certificate up to November 2026, and "GoDaddy TLS Intermediate CA DV - R1v1",
which issued the one served since 10 November 2026. With it comes "GoDaddy TLS
Root CA - R1" cross-signed by "Go Daddy Root Certificate Authority - G2", since
no CA bundle carries the R1 root yet (certifi 2026.10.23 does not). They are
GoDaddy's `gd_bundle_dv-r1-g2.crt.pem` from certs.godaddy.com/repository.

## The excise law (Justel)

`excise_law.py` reads the residential special excise from the consolidated
text of article 419 of the programme law of 27 December 2004:

- `GET https://www.ejustice.just.fgov.be/cgi_loi/change_lg.pl?language=fr&la=F&table_name=loi&cn=2004122730`,
  in French and ISO-8859-1;
- under natural gas, "consommation non professionnelle", "b) autres", the
  two slices (up to and above 12 MWh), each a rate followed by the steps
  already voted ("A partir du 1er janvier 2027: ...");
- under "a) client protégé résidentiel", the one rate a protected household
  pays on every kWh, with its own steps (3,09 EUR/MWh from 1 August 2026,
  then 3,42, 3,74 and 4,08 each 1 January), which only the social tariff
  bills; a page whose protected paragraph cannot be read still gives the
  household rates, and the live check fails on it;
- the day the rate in force took effect is the entry into force of the
  amendment bracket around it (1 August 2026);
- beside each slice's excise, the energy contribution ("cotisation sur
  l'énergie"), with the same steps: zero in the wording of 1 August 2026 and
  in every step after it. The two slices have to agree on it, since a bill
  levies one; a page whose contribution cannot be read still gives the
  excise.

A page that is not the article as expected (another band, a rate past
0,05 EUR/kWh, a missing slice) is refused whole. It is read in a worker
thread, in time linear in the page, once a day; a failure waits six hours,
and what was read is kept in each entry's Store.
It bills a month FPS Finance's excise tariff does not list yet, below. The
live check reads the same page and fails when most of the fleet prints
another excise than the one billed.

## FPS Finance's excise tariff (TarBel)

Article 420 §3/1 moves both household slices every quarter on the quarter's
gas price, by a notice in the Moniteur belge that Justel does not
consolidate. `tarbel.py` reads the rates in force from the workbook FPS
Finance publishes for excise declarations (annex 7):

- `GET https://eservices.minfin.fgov.be/extTariffBrowser/FileResourceForHomePageServlet?fname=accijnzen.xlsx&lang=NL`,
  in Dutch, about 280 KB, its unpacked size capped at 16 MiB and read as
  every workbook is (`xlsx.py`, under Fluvius's sheet below);
- sheet `Q`, the rows "Aardgas gebruikt als verwarmingsbrandstof voor
  niet-zakelijk gebruik, m.u.v. de beschermde residentiële afnemer", "van 0
  tot 12 MWh" or "vanaf 12 MWh", "van toepassing vanaf ... tot en met ...",
  in EUR per MWh, the ordinary and the special excise (codes 100 and 200)
  added, and the columns found by their headers;
- the energy contribution, code 300: up to July 2026 on rows of its own
  ("enkel de bijdrage op de energie", "van toepassing tot en met
  31/07/2026", no first day), 0,9978 EUR/MWh, while the excise rows of those
  quarters say "enkel de bijzondere accijns" and carry 0 under that code,
  which is not read as the contribution; on the rows that carry both, August
  and September 2026, zero. A month no row gives is left to the law. Rows
  that cannot be read leave the excise read;
- the protected customer's rows, "beschermde residentiële afnemer", one per
  period and without slices: exempt from April to June 2023, 2,77 EUR/MWh
  from July 2023 to July 2026, 3,09 from August 2026. Article 420 §3/1 does
  not move that rate, so the law's wording bills it from the day it dates,
  and this tariff the months before. Rows that cannot be read leave the
  household slices read, and the live check fails on them.

The version of 1 August 2026 lists April 2023 to December 2026, the quarters
`const.py` types among them, figure for figure. A workbook that is not as
expected (a slice or a period missing, another unit, overlapping periods) is
refused whole. It is read once a day, a failure waits six hours, what was
read is kept in each entry's Store, and a period the workbook later drops
stays held. A period listed with no end, as the protected rate's from August
2026 is, covers the rest of its first year only: article 419 sets a new rate
each 1 January, which only Justel gives. A month it lists is billed on it
before anything else; the live check reads it too.

## Fluvius's gas tariff sheet

`fluvius.py` reads the data management fee of a yearly-read meter
("Tarief databeheer", row "Jaaropname (o.a. digitale gasmeter G4 en G6 ...)")
off Fluvius's yearly tariff list, which the VREG approves:

- `GET https://www.fluvius.be/nl/factuur-en-tarieven/netkosten/netkosten-afname-aardgas`
  lists a publication page per area and year,
  `/nl/publicatie/<area>-distributienettarief-afname-aardgas-0101<year>-3112<year>`;
- the first one of the year links its workbook,
  `/sites/fluvius/files/.../*.xlsx`, read with the standard library's zip
  reader and defusedxml, its unpacked size capped at 4 MiB.

Each workbook read here, Fluvius's, the CWaPE's and FPS Finance's, is read
in a worker thread, and nothing its zip declares is trusted: the zip has to
end with its directory, at most 64 KiB, with no comment or ZIP64 record, a
member has to be stored or deflated, and each member is read through
`ZipFile.open` no further than what is left of the cap, which is as far as
it is inflated.

Every area prints the same fee (17,51 EUR/year excluding VAT in 2025,
17,85 in 2026), so one sheet stands for all eight. A sheet that is not the
year's gas tariff list, or whose fee row holds no single figure between 1
and 100 EUR, gives nothing. A year once read is kept for the process and in
each entry's Store; a failure waits six hours.

## The network sheets a custom entry takes its network from

A custom entry that ticks **Take them from my operator's published tariff
sheet** on its network step takes its T1 and T2 terms and its metering off
its regulator's sheet of each delivery year (`network_sheet.py`), put on the
month's VAT, instead of the figures typed, which only stand in while no
sheet is held. The transport stays as typed: no sheet prints it. Each sheet
is read in the tick for the current year, the year before's standing in
until the year's is published, and for an earlier custom contract of the
year at its DSO; nothing of it is kept over a restart, and a failure waits
six hours. For a month whose own year's sheet is not held, what the card
archive kept of that month's sheet stands in (`network.json`, below), then
the latest earlier sheet held, all ahead of the figures typed
(`network_sheet.held_network`). The earlier year's sheet comes after the
archive's row because the read of the year's sheet may have failed on the
installation while the archive's run read it.

Every sheet prints a fixed term a year and a rate per kWh per tier, then the
public service, pension, levy and regulatory balance rates per kWh, which
the cards add into each tier's rate. Those are summed whatever each year
calls them (Fluvius's levy row is "Overige lokale, provinciale, ... en
retributies" in 2024 and "Retributies en heffingen van publieke overheden"
in 2026), so a row a sheet stops printing is billed as gone. With 6% VAT each
gives what every card of October 2026 prints for its DSO, within the cards'
rounding: eleven DSOs, five figures each.

The live check reads every sheet the same way each day and holds its figures
against what the cards print for the DSO (see
[ci-and-testing.md](ci-and-testing.md)).

- **Flanders** (`fluvius.py`, `parse_area_network`): the entry's own area's
  sheet off the listing above, `fluvius-<area>-distributienettarief-afname-aardgas-0101<year>-3112<year>`;
  the "Vaste term" and "Proportionele term" of the T1 and T2 columns, every
  other row in EUR/kWh down to the remarks, and the "Jaaropname" fee. On
  Antwerpen for 2026: 14,79 and 78,51 EUR a year, 0,0212869 and 0,0085442
  EUR/kWh, 17,85 EUR a year.
- **Brussels** (`brugel.py`): Brugel's final sheet,
  `https://brugel.brussels/publication/document/notype/<year-1>/fr/Tarif-distribution-Gaz-<year>.pdf`,
  "prix hors TVA": the "X = EUR / an" and "Y = EUR / kWh" rows, every row in
  EUR / kWh below them, and the "Comptage YMR" term of a yearly-read meter,
  its text read in a worker thread, a figure starting where a number does.
  For 2026: 15,00 and 40,63 EUR a year, 0,0187738 and 0,0136483 EUR/kWh,
  23,54 EUR a year. Only 2026's was served on 6 November 2026.
- **Wallonia** (`cwape.py`): the operator's approved grid, found through the
  CWaPE's documents service, `https://www.cwape.be/rest/documents?_format=json`
  with `documents_types[]=203` (ORES is operator 453, RESA 225, and each file
  must be named after its operator), read off its workbook. Rows are keyed on
  their market code: under `G140` the "Fixe" row and the "gaz acheminé par
  conduites" rate, then every other row in EUR/kWh (`G145`, `G861`, `G850`,
  `G860`, `G410`); RESA's "supplément pour gaz porté", billed only where gas
  comes by road, is left out. Wallonia bills no metering term. Each grid
  states the days it is valid, and the one valid on the delivery month is
  billed, else the latest begun. On ORES for 2026: 30,10 and 132,95 EUR a
  year, 0,0404664 and 0,0208089 EUR/kWh.

## The card archive

The `archive_cards` workflow stores every card it parses daily in the
be_price_cards repository, under `gas/cards/<supplier>/<contract>/<region>/<YYYY-MM>.json`.
A card published as page images (Ecofix's) is read there with the OCR engine
[ocr_price_cards](https://github.com/renaudallard/ocr_price_cards), and each
source it read records the engine version (`"ocr"` in `_sources`).
An installation reads the archive (`month_cards.fetch_archived_card`, and
`fetch_archived_row` for the OCR flag) for a past month, before the
supplier's own archive, which costs a PDF and a parse per month, for the
running month of a card published as images
(`month_cards.current_card`), and when its current card cannot be read at
all or has gone stale. The request names the supplier, the contract,
the region and the month and nothing else; the *Read the project's card
archive* option switches it off. A row past 256 KiB is refused as it
streams (the largest of the 3,037 held on 5 November 2026 was 4 kB), and so
is a table below past 1 MiB (network.json, the largest, was 42 kB).

Beside the cards, the workflow writes `gas/vat.json`: per card month, the
residential VAT rate three suppliers or more state and none contradicts.
`vat_rates.py` reads it once a day for an entry with the card archive
option on (`GET https://raw.githubusercontent.com/renaudallard/be_price_cards/main/gas/vat.json`,
a failure waiting six hours) and keeps it in each entry's Store, a month from
2000 to next year alone (`law_archive.table_months`); an entry with the
option off reads nothing and bills on the table it last read. A month is
billed on its own entry, or on the last month before it that agreed; before
the table has been read, or for a month before the first it holds, on the
law's 6 % in `const.py`. That rate grosses the law's figures on a card that
states none, a formula a parser grossed by 6 % for want of a stated rate,
and the figures a household typed from its contract.

It also writes `gas/law.json`: per month from January 2025 up to the
current one, what the integration bills on from the law, the excise of
both slices, the protected customer's rate and the energy contribution,
read from FPS Finance's tariff and Justel the way the live check reads
them, with an installation's readers and a network failure tried again
after a pause. A month they still give is written again, figure by figure; one
they no longer give keeps what was written. `law_archive.py` reads it once
a day for an entry with the card archive option on
(`GET https://raw.githubusercontent.com/renaudallard/be_price_cards/main/gas/law.json`,
a failure waiting six hours), keeps it in each entry's Store, a month from
2000 to next year alone, and answers only for a month the entry's own
readings do not give, ahead of the figures `const.py` holds.

And it writes `gas/network.json`: per month of last year and this one, and
per DSO, what each regulator's network sheet gives a custom entry taking its
network from it, read with an installation's readers, each figure to the
seventh decimal. A month the sheets still give is written again; one they no
longer give keeps what was written, and months past the rows' retention go.
`network_archive.py` reads it once a day for an entry with a contract taking
its network from a sheet and the card archive option on
(`GET https://raw.githubusercontent.com/renaudallard/be_price_cards/main/gas/network.json`,
a failure waiting six hours), keeps it in such an entry's Store, a month
from 2000 to next year alone, and answers only for a month the entry's own
reading of that year's sheet does not give, ahead of an earlier year's sheet
and of the figures typed.

## The recorder

`gas_meter.py` reads the gas meter's long-term statistics, daily `change` in
local days, converted by the recorder to m³ (a volume meter) or kWh (an
energy meter). A unit Home Assistant cannot convert is refused rather than
read as m³. A negative daily change is a replaced or re-based `total` meter
and counts as none. The cost history reads the hourly changes as recorded, a
negative one included, so that a day's hours add up to its change.

## Postcodes

`scripts/refresh_postcodes.py` builds `postcode_map.py` from bpost's
postcode list and Synergrid's DSO lookup: for each of the 1 146 postcodes
with a province, and for a postcode bpost lists without one where an
override names it (SHAPE's 7010), the gas DSO of every locality Synergrid
files under it.
Where Synergrid disagrees with Fluvius's open data, the Flemish regulator or
CWaPE's per-postcode table, `OVERRIDES` in the script corrects the entry,
each with its evidence in a comment (Voeren has no gas network although
Synergrid names Fluvius Limburg; Andenne is RESA, not ORES; eighteen Walloon
postcodes Synergrid leaves without gas are ORES, and so is SHAPE, where ORES
counts some 550 residential gas access points). Run it once a year: DSOs
merge and communes move between them.

`postcodes.resolve` gives the region and the postcode's DSOs, and the setup
flow offers only those. A postcode with no designated gas DSO (260 of them,
nearly all in Wallonia) is refused with `no_gas_network`; clearing the
postcode lets the household pick region and DSO itself.

A gas DSO Synergrid names that has no key goes to `UNPRICED` in the
generated module instead. The one there is Enexis in Baarle-Hertog (2387):
the postcode resolves to Fluvius Kempen, which serves Zondereigen, and the
setup flow's `unpriced_network` step warns that the rest of the commune is on
a Dutch network the integration does not price.
