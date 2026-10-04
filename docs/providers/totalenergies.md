# TotalEnergies

Module: `custom_components/be_gas_prices/providers/totalenergies.py`. Tests:
`tests/test_totalenergies.py`, fixtures in `tests/fixtures/totalenergies/`.

## Where the cards are

The listing, https://totalenergies.be/fr/particuliers/electricite-et-gaz/cartes-tarifaires,
links one PDF per product and region at a URL that is overwritten every
month:

    https://totalenergies.be/static/marketing-documents/b2c/tariff-card/latest/<SLUG>_GAS_<VL|WAL|BXL>_FR.pdf

It is the electricity pattern with `_ELECTRICITY_` replaced by `_GAS_`. The
URLs are fixed, so `fetch` builds them and does not read the listing. The
listing also links `IMPACT_GAS_VL_FR.pdf` and `IMPACT_GAS_BXL_FR.pdf`, but
both answer 200 with an HTML page (a soft 404), as does any card that does
not exist. `fetch_pdf_text_layout` refuses them by their magic bytes.
`TARIFF_SOCIAL_GAS_ALL_FR.pdf` is the CREG social tariff and is not
registered. myDrive and myDynamic have no gas card.

`probe` sends a HEAD to the current card and returns its `Last-Modified`. The
September 2026 cards all carry 1 September 2026 06:10 to 06:31 GMT.

## Products and regions

| Contract id | Label | Kind | Slug | Regions |
|---|---|---|---|---|
| totalenergies_gaz_fixed | TotalEnergies Gaz Fixe | fixed | GAZ-FIXE | V W B |
| totalenergies_gaz_variable | TotalEnergies Gaz Variable | indexed (TTF_M_RLP) | GAZ-VARIABLE | V W B |
| totalenergies_impact_variable | TotalEnergies Impact Variable | indexed (TTF_M_RLP) | IMPACT | W |
| totalenergies_mycomfort_variable | TotalEnergies myComfort Variable | indexed (TTF_M_RLP) | MYCOMFORT | V W B |
| totalenergies_mycomfort_fixed | TotalEnergies myComfort Fixe | fixed | MYCOMFORT-FIXED | V W B |
| totalenergies_myessential_variable | TotalEnergies myEssential Variable | indexed (TTF_M_RLP) | MYESSENTIAL | V W B |
| totalenergies_myessential_fixed | TotalEnergies myEssential Fixe | fixed | MYESSENTIAL-FIXED | V W B |

The label is the product name as the card heading prints it, after
"TotalEnergies". The energy price differs by region: the formula's base
(0.704 in Wallonia, 0.604 in Flanders and 1.42 in Brussels on myComfort
Variable in September 2026) and the fixed price (8,45 / 8,35 / 9,21 on
myComfort Fixe). The region's own card is therefore read.

## How each figure is read

The cards are read with pdfplumber (`fetch_pdf_text_layout`), which keeps
each DSO row on one line. pypdf prints all the DSO labels first and their
figures after, and it misses the last row of the index history. pdfplumber
takes about 10 s per card on a Raspberry Pi 4 (`sweep_cost_s`).

- Heading: both pages print `TotalEnergies <product>` on one line and the
  card month on the next (`septembre 2026`). Every heading must name the same
  product and month. The product must be the contract's own, so a card
  served for another product is refused. `publication_label` and
  `valid_until` come from this month.
- Side stamp: every page carries a rotated stamp with a production date and
  the file name. It extracts reversed (`erbmetpes`, `5202`,
  `RF_ZAG_elbairaV`, `trofmoCym_LAWR-C2B`), and its date has nothing to do
  with the card: `18 septembre 2025` on myComfort, `février 2026` on Gaz Fixe
  and Gaz Variable. No pattern here matches it.
- VAT: the heading reads `TVA 6 % incluse`, but the 6 is a separate text
  field. The layout reader prints `TVA % incluse` and puts the `6` on a line
  of its own after `Conditions particulières relatives à la formule ...`.
  `_VAT_RE` requires both. The rate goes to `card_vat_rate` and is used to
  gross up the formula. It is mandatory.
- Fixed energy: `90,00 8,45 Tarif annuel`. The yearly fee (€/an) comes
  first, then the price (c€/kWh). The column positions were checked on the
  page: 90,00 sits under "Redevance fixe (€/an)" and 8,45 under
  "Consommation (c€/kWh)".
- Variable energy:
  - `7,41 Tarif mensuel` is the estimate made with the Vlaamse Nutsregulator
    method (`8.03`, with a dot, on the October 2026 myEssential card). It is not stored while the indicative price below is printed.
    The line under it (`90,00`, from October 2026 `100`) is the yearly fee.
  - `0.1007 * TTF_M_RLP + 0.704 Formule tarifaire` is the formula in c€/kWh
    excluding VAT, with the index in €/MWh. The March 2026 cards print it as
    `0.1007*TTFM_RLP+0,67`, and February 2026 uses comma decimals. The
    regex accepts all three.
  - Factor and base are divided by 100 and grossed up by the VAT rate.
  - `Compteur Simple : 7,33 € cent/kWh`, printed under "A titre indicatif, ...
    la dernière valeur connue du TTF_M_RLP (du mois précédent)", is
    `IndexedRates.price`. `settled` is False, because TTF_M_RLP is only known
    once the delivery month is over. The first issue of the October 2026 Gaz
    Variable cards, on 1 October, left it blank (`Compteur Simple : € cent/
    kWh`), and the estimate was then the price. The reissue later that day
    prints it again (8,78 against an estimate of 8,12 in Wallonia), and the
    estimate still stands in for a card leaving it blank.
- DSO table: it runs from `Coûts de distribution - Terme variable` to
  `Accise fédérale`. The columns are all proportional terms first (0-5000,
  5001-150.000 and 150.001-400.000 kWh, c€/kWh), then the three fixed terms
  (€/an), `Transport` (c€/kWh), `Coût activité de mesure et comptage` (€/an),
  then the levies. Wallonia has 10 columns, Flanders and Brussels 9.
  - Wallonia: `RESA SA` and five ORES rows (`ORES (Namur - Namen)`, `(Hainaut
    - Henegouwen)`, `(Luxembourg - Luxemburg)`, `(Waals-Brabant Wallon)`,
    `(Mouscron - Moeskroen)`). The ORES rows must agree. Metering prints
    0,00.
  - Flanders: the eight `Fluvius <area>` rows, in alphabetical order, with
    labels matching their figures. Halle-Vilvoorde carries 17,59 / 2,50, the
    regulator's figures for that area. Metering is 18,92.
  - Brussels: `SIBELGA`, metering 24,96.
- Energy contribution: the last column of the table (`Cotisation sur
  l'énergie`, 0,11 c€/kWh). It is read from every row, and the rows must
  agree. A dash would be read as zero.
- Walloon connection fee: the column before the contribution (`Redevance de
  raccordement`, 0,01 c€/kWh), read the same way. It is mandatory, so a
  dash raises.
- Excise: `Consommation entre 0 et 12.000 kWh 1,09` and `> 12.000 kWh
  1,18` under `Accise fédérale`. Both are read as printed.
- Brussels levy: after `Droit pour le financement des Obligations de Service
  Public €/an`, one row per caliber, from `<= 10 m³/h 5 3,56` to `> 160 m³/h
  970,41`. The amount is the row's last figure. The `5` and `6` after the
  smallest caliber are footnote marks the card never defines. The nine
  amounts go to `osp_table`.

## Index values

`fetch_index` reads the PDF linked from
https://totalenergies.be/fr/particuliers/produits-et-services/my-home/electricite-et-gaz/offre-electricite-gaz/valeur-des-indices-historique:

    https://totalenergies.be/fr/files/Historique-valeurs-paramètres-gaz-produits-actuels-FR.pdf

The URL redirects to a CDN copy under a dated folder
(`s3fs-public/2026-09/...`), so the link on the page is the one kept. Its title
reads "Paramètres gaz myComfort, myEssential, Impact, Gaz". It gives one row
per delivery month from 06/2024, newest first, in €/MWh excluding VAT:
`08/2026 61,662 61,824` for the columns `TTF_M_RLP* ZTP_M_RLP* ZTP_Q_RLP**`.

The header is checked, because the columns are read by position. The
quarterly ZTP_Q_RLP is printed on every month of its quarter and stays empty
until the quarter is over. A two-figure row is therefore missing the last
column. On 2026-09-29 the last row was 08/2026.

Every current card indexes on TTF_M_RLP. ZTP_M_RLP and ZTP_Q_RLP are
published in the same table and returned too. The history of older products
is in a separate PDF linked from the same page
(`...-gaz-anciens-produits-FR.pdf`), which is not read.

The card's indicative price is its own formula at the previous month's
TTF_M_RLP from this table, to its two decimals. For example, (0,1007 x 61,662
+ 0,704) x 1,06 = 7,33. This holds on the ten September 2026 variable cards
and on eleven of the twelve archived variable cards checked, from October
2025 to August 2026. The exception is described below.

## Archive

The archive pattern is

    https://totalenergies.be/static/marketing-documents/b2c/tariff-card/<YYYY>_<M>_<SLUG>_GAS_<REG>_FR.pdf

with the month not zero-padded. `2026_08_...` is a soft 404, and so is any
month outside the archive. The current month is there too: `2026_9_...` is
byte for byte the `latest/` card. `fetch_for_month` does not ask for a month
after the current one in Home Assistant's zone. It returns None on a soft
404, raises on a transient failure, and refuses a card whose heading names
another month.

Where the archive starts (checked on the Walloon cards on 2026-09-29):

- the my* products from `2025_10` (`2025_9` is a soft 404);
- Gaz Fixe and Gaz Variable from `2026_3`;
- Impact from `2026_4`.

The history page for Wallonia,
https://totalenergies.be/fr/particuliers/produits-et-services/my-home/electricite-et-gaz/offre-electricite-gaz/historique-des-cartes-tarifaires-wallonie,
links only the Walloon my* cards and older products (Pixel, Pixie, iChoosr).
The other products and regions are served all the same.

Archived copies can be uploaded again later. The January 2026 myComfort
cards carry `Last-Modified` 6 and 9 March 2026. On the Flemish one, the
indicative 4,26 is the formula at January's own TTF_M_RLP (34,263), not at
December's (27,652, which gives 3,56). For an indexed contract the engine
prices a past month on the published index, so only the printed fallback is
affected.

## Card quirks and errors

These were seen on the September 2026 cards and compared with
`tmp/research/regulated/FINDINGS.md` and the other suppliers' cards.

- Every regulated figure is rounded to two decimals:
  - excise 1,09 / 1,18 (law: 1,09286 / 1,18296);
  - transport 0,17 (Fluxys: 0,16536);
  - the Walloon connection fee 0,01 (regulated: 0,0075 c€/kWh, printed as
    such by Engie, Luminus, Mega and others);
  - the distribution terms (ORES 4,29 for 4,28944).

  The excise is replaced by the law's rates for the delivery months
  `const.py` knows the law for, August to December 2026
  (`_resolve.resolve_federal_levies`), and so is the Walloon connection fee,
  billed at the law's 0,0075 c€/kWh where the card prints 0,01
  (`_resolve.resolve_connection_fee`). Transport and the distribution terms
  are billed as printed.
- The energy contribution is still printed at 0,11 c€/kWh. It has been 0
  since 2026-08-01, and was 0,105767 before that. The law override sets it to
  zero for delivery months from August 2026.
- Fluvius Kempen's T1 fixed term is 16,16 €/year. The Fluvius tariff sheet
  and Engie's card have 16,17.
- Sibelga's metering fee is 24,96 €/year. The regulated figure in
  FINDINGS.md and Engie's card have 24,95.
- The third tier is headed `150.001-400.000 KWh`, while the regulated T3
  runs to 1.000.000 kWh. The terms printed are the regulated T3 ones (ORES
  889,48 / 1,64).
- The footnote on `Accise fédérale 4` describes the federal contribution
  ("Le produit de cette cotisation fédérale ..."), not the excise.
- The formula text says "le prix de l'électricité sera déterminé", a wording
  left over from the electricity card.
- The Walloon heading reads "Gaz en Région de Région wallonne".
- Every card carries an ETS2 clause from 1 January 2027 (a cost per MWh at
  an emission factor and quota price not yet set). It is not priced.

## Tests

`tests/test_totalenergies.py` reads the real cards with the layout reader.
The fixtures are the September 2026 cards (all seven Walloon products,
myComfort Variable and myComfort Fixe in Flanders and Brussels), the archived
`2026_8_MYCOMFORT_GAS_WAL_FR.pdf` and `2026_3_GAZ-VARIABLE_GAS_WAL_FR.pdf`,
the October 2026 `2026_10_GAZ-VARIABLE_GAS_WAL_FR.pdf` and
`2026_10_MYESSENTIAL_GAS_WAL_FR.pdf`, and the index history PDF. The tests pin:

- the variable leg on myComfort Wallonia: index, factor, base, formula
  string, indicative price, fee, month and VAT;
- the first issue of the October 2026 Gaz Variable card, whose indicative
  price is blank, priced at its estimate;
- the October 2026 myEssential card, whose estimate has a dot;
- the base per region and the fee and formula of each variable product;
- the price and fee of every fixed card fixture, in all three regions;
- the formula check: on eight variable cards, the formula at the previous
  month's TTF_M_RLP from the history reproduces the printed price to
  0,005 c€/kWh;
- the March 2026 formula spelling and its stale excise 0,87 / 0,96;
- the DSO tables: RESA and ORES in full, the Fluvius Kempen row in full
  (16,16 included) with its 18,92 metering, and the Sibelga row with its
  24,96 metering;
- the levies as printed: excise bands, contribution 0,11, connection fee
  0,01, and the nine Brussels OSP amounts;
- refusals: another product's card, another region's card, Impact outside
  Wallonia, an unknown contract, and a card missing the VAT, the formula,
  the estimate, an excise row or a DSO row;
- the index history: values of all three indices, the quarter filling, an
  empty current quarter, the depth from 06/2024, and refusal of a reordered
  header;
- `fetch`, `probe` and `fetch_for_month`: the URLs built (unpadded month),
  refusal of another month's card, the transient raise, the soft 404 as no
  card, the Brussels clock at a month boundary, and no request for a future
  month or an unsold region.
