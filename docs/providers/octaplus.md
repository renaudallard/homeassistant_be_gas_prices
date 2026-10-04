# OCTA+

Module: `custom_components/be_gas_prices/providers/octaplus.py`. Tests:
`tests/test_octaplus.py`, fixtures in `tests/fixtures/octaplus/`.

## Where the cards are

- Card: `https://files.octaplus.be/tariffs/G_OCTA_<SLUG>_RE_<VL|WL>_FR.pdf`,
  the electricity pattern with `G_` for `E_`. One card per product and
  region, overwritten in place every month.
- The tariff page https://www.octaplus.be/fr/electricite-gaz-naturel/tarifs
  links every card. Its Brussels gas links are professional cards only
  (`_PR_BR_`); the residential `_RE_BR_` names answer 404. Residential gas is
  sold in Flanders and Wallonia.
- The next month's card may go up on the last day of the month before (31
  August 2026 13:26 for every product, 30 September 2026 14:13 for Smart
  Variable, Brussels time). `fetch` returns the card as it stands online,
  which the card archive stores under its own month. Until that month
  begins, an installation is priced on the running month's card from the
  archive instead (`month_cards.current_card`, which does so for any
  supplier with an archive), and on the card online only when the archive
  has none.
- Probe: HEAD Last-Modified of the card (for example "Mon, 31 Aug 2026
  11:26:04 GMT" on the September cards), which moves when the card is
  replaced, and the running month, so the card put up early is read again
  once its month begins.
- Reader: pdfplumber layout (`fetch_pdf_text_layout`), which gives one table
  row per line. pypdf puts each DSO value on its own line.

## Products and regions

| Contract id | Label | Slug | Kind | Index | October 2026 (WL) |
|---|---|---|---|---|---|
| `octaplus_basiconline` | OCTA+ Basic Online | BASICONLINE | indexed, monthly | ZTP RLP M | 7,68 c€/kWh, 40,00 €/year |
| `octaplus_boostflex` | OCTA+ Boost Flex | BOOSTFLEX | indexed, monthly | ZTP RLP M | 7,82, 65,00 |
| `octaplus_ecoboostflex` | OCTA+ Eco Boost Flex | ECOBOOSTFLEX | indexed, monthly | ZTP RLP M | 9,20, 150,00 |
| `octaplus_smartvariable` | OCTA+ Smart Variable | SMARTVARIABLE | indexed, monthly | ZTP RLP M | 9,49, 160,00 |
| `octaplus_boostfix` | OCTA+ Boost Fix | BOOSTFIX | fixed | | 8,81, 110,00 |
| `octaplus_ecoboostfix` | OCTA+ Eco Boost Fix | ECOBOOSTFIX | fixed | | 10,02, 150,00 |

- OCTA+ withdrew Flux, Eco Flux, Fixed and Eco Fixed with the October 2026
  range, and they were removed; their card URLs went on serving the
  September cards. An entry on one of them stops at setup with that reason
  (`contract_withdrawn`) until its settings name another contract.

- Regions: flanders and wallonia for every product.
- The VL and WL cards print the same energy figures. Both print the DSO tables
  of both regions; only the WL card has the connection fee column.
- Smart Variable is a one-year contract ("Durée du contrat 1 an"); the others
  are open-ended.

## How each figure is read

- Product: the card's first line, "GAZ SMARTVARIABLE", "GAZ BOOST FLEX" or
  "GAZ ECO BOOST FIX", must name the contract's slug once its spaces are
  dropped, or the parse fails.
- Card month: the banner "FICHE TARIFAIRE SEPTEMBRE 2026" gives
  `publication_label` and `valid_until` (last day of that month). The fixed
  cards print no "valables pour tout contrat signé du ... au ..." sentence,
  so the banner is the only date every card has.
- Energy: "Redevance fixe (€/an) 65,00" and "Coût du gaz (c€/kWh) 6,89".
- Formula (variable cards only), read after "ZTP RLP M":
  - Boost Flex: "La formule de prix est la suivante, en EUR/MWh HTVA : ZTP RLP
    M * 1,020 + 3,200." Basic Online: "* 1,020 + 1,940", Eco Boost Flex:
    "* 1,010 + 16,910". The withdrawn Flux printed "* 1,010 + 2,160".
  - Smart Variable: "La formule tarifaire est : ZTP RLP M* 1,15+ 10 EUR/MWh."
    It does not say HTVA.
  - `factor` = 1,020 / 1000 x 1,06 and `base` = 3,200 / 1000 x 1,06, in
    EUR/kWh with the index in EUR/MWh.
- VAT: the cards print "TVAC" and no rate, so `card_vat_rate` is None and the
  formulas are grossed up by the statutory 6% (`const.VAT_RATE_REDUCED`).
- Price: the only price on a variable card is "prix moyen estimé pour les 12
  prochains mois", computed on the forward "V-test" value OCTA+ publishes at
  octaplus.be/prixattendus (`paramètres_forward_fr.pdf`, column "ZTP
  Moyenne pondérée"). The card prints no figure at the last known index, so
  `IndexedRates.price` is that estimate. `settled` is False: ZTP RLP M is
  only known once the delivery month is over.
  - Check: V-test 09/2026 = 62,209 EUR/MWh gives Flux 6,889, Eco Flux 8,771
    and Smart Variable 8,643 c€/kWh with 6% on top, against 6,89, 8,77 and
    8,64 printed. Smart Variable only reproduces grossed up, so its formula is
    excluding VAT too. June (44,244: Flux 4,97, Smart Variable 6,45) and July
    (39,377: Flux 4,44) reproduce the same way.
- DSO table, "LES TARIFS DES RÉSEAUX (c€/kWh)": columns 0-5000 kWh terme fixe
  (€/an) and terme proportionnel (c€/kWh), 5001-150 000 kWh the same, Tarif
  Gestion données (€/an, "-" in Wallonia), Transport (c€/kWh). No T3.
  - Flanders: the rows between "Région flamande" and "Région wallonne",
    labelled as `FLUVIUS_LABELS` spells them, e.g. "Fluvius West 19,03 2,69
    101,02 1,05 18,92 0,165".
  - Wallonia: the rows between "Région wallonne" and "LES SURCHARGES": RESA
    and the five ORES sub-areas, which must agree, e.g. "RESA 34,59 4,64
    122,05 2,53 - 0,165".
  - Proportional terms are rounded to two decimals (2,69 where Fluvius
    publishes 2,69459).
- Levies, "LES SURCHARGES (c€/kWh)", columns Droits d'accise spéciaux,
  Cotisation sur l'énergie and, on the WL card, Redevance raccordement
  Wallonie (checked by word position):
  - "Consommation entre 0 & 12.000 kWh 1,0929 0,1058 0,0075": excise first
    slice, energy contribution, connection fee.
  - "Consommation > 12.000 kWh 1,1830 0,00": excise second slice, energy
    contribution.
  - The connection fee is mandatory on a Walloon card: a WL card without it,
    or a VL card read for Wallonia, fails the parse.
  - No Brussels levy: there is no residential Brussels card.

## The index and where its values come from

- https://www.octaplus.be/prixgaz answers 308 to
  `https://files.octaplus.be/tariffs/param%C3%A8tres_gaz_fr.pdf`. The module
  asks the short link, which is what the cards name.
- The PDF is a table "Periode TTF101 Mois TTF103 Trimestre TTF RLP Mois ZTP
  RLP Mois ZTP RLP Trimestre", one row per delivery month over a rolling
  window (10/2024 to 12/2026 on 2026-10-04), e.g. "08/2026 53,734 45,112
  61,729 61,899 -". A month not over prints "-".
- `parse_index` checks that heading and returns the ZTP RLP Mois column as
  `{"ZTP RLP M": {"2026-08": 61.899, ...}}` in EUR/MWh. The other columns are
  no gas card's index and are not read.
- Definitions differ slightly: the PDF and the Smart Variable card say ZTP
  DAP quotes weighted by the Synergrid RLP; the Boost Flex, Eco Boost Flex
  and Basic Online cards, like Flux before them, say EGSI ZTP indices
  weighted by the RLP.
- Oddities in the PDF, in columns not read: TTF101 prints 52,974 for both
  03/2026 and 04/2026, and ZTP RLP Trimestre prints 45,625 for 06/2026 against
  45,624 for 04 and 05/2026.

## The archive

- Listing: `https://srv.octaplus.be/websiterest/getTarifArchive?Lang=FR&Region=<VL|WL>&AnneeMois=YYYYMM&Nrj=G&Canal=website&TypeContrat=RE`.
  JSON `{"Response": [{"NomPdf": "2026-10 G OCTA+BOOSTFLEX RE WL FR.pdf", ...}]}`.
  Region=BR is an empty list; a month not published yet (202610 on
  2026-09-29) too.
- Card: `https://srv.octaplus.be/websiterest/getTariffSheet?Canal=website&RequestedPDF=<name>`
  answers `{"Response": {"Ok": "True", "TariffSheet":
  "data:application/pdf;base64,..."}}`. A name it does not hold is answered
  200 with `{"Ok": "False", "Message": "E-ECP"}`, which reads as no card. A
  sheet that is not base64, ASCII or not, is an `ExtractorError`.
- The bytes go through `_pdf.render_pdf("layout", ...)`, so the card archiver
  sees them like any card fetched by a reader.
- Names: the March 2026 listing named the fixed cards FIXEDD and ECOFIXEDD
  (the Fixed one a revision "valable à partir du 16/03/2026"); a unique name
  that differs only by a doubled letter is accepted. The sheet endpoint also
  still serves the first March Fixed card under the plain name, but the
  listing does not name it, so it is not read.
- Products listed since 2023-01 (WL and VL alike): CLEAR and ECOCLEAR
  (2023-01 to 2025-12), FIXED and ECOFIXED (from 2023-07), SMARTVARIABLE
  (throughout), FLOW and ECOFLOW (2025-02 to 2025-12), FLUX (from 2025-07),
  ECOFLUX (from 2026-01); from 2026-10 BOOSTFIX, BOOSTFLEX, ECOBOOSTFIX,
  ECOBOOSTFLEX and SMARTVARIABLE, Basic Online not listed in that month.
- Template: cards from June 2026 on are read. The cards checked from
  September 2025 to May 2026 use an older template ("Tarif : Flux", "Clients
  résidentiels en Wallonie - 05/2026 - Tarifs 6% TVAC") set in spaced glyphs,
  which pdfplumber reads column by column. `fetch_for_month` returns None for those months, and the month
  cache falls back to the card archive. Seen on that template: the Walloon
  connection fee printed 0,075 c€/kWh (ten times the regulated 0,0075), a
  transport heading in c€/MWh over c€/kWh values, and a stated 6% rate.
- A card naming another month than the one asked for is refused.

## Known quirks and card errors

- Energy contribution: the August and September 2026 cards print 0,1058
  c€/kWh on the first excise slice and 0,00 on the second; June and July
  printed 0,1058 on both. The law zeroed it from 1 August 2026. The card is
  read as printed (the first slice's figure) and `_resolve` applies the
  delivery month's law.
- July 2026: the Flux WL card already printed the August excise (1,0929 /
  1,1830) while the Fixed VL card still printed 0,8724 / 0,9623.

## Tests

`tests/test_octaplus.py`:

- Boost Flex WL: index, factor, base, formula text, estimate, fee, month,
  VAT basis. Eco Boost Flex and Basic Online VL: formula, estimate and fee.
  Smart Variable VL (September): its formula spelling, estimate and fee.
- The printed estimate equals the formula at the V-test value read from
  `paramètres_forward_fr.pdf` (Smart Variable, September).
- Boost Fix WL and Eco Boost Fix VL: price and fee.
- Flanders table (Fluvius West in full, Kempen) and Wallonia table (RESA and
  ORES in full, five ORES rows collapsed).
- Levies on the WL and VL cards; a VL card read for Wallonia, Brussels,
  another product's card and a card without its formula all fail.
- `parse_index` on `paramètres_gaz_fr.pdf`, and its heading check.
- `fetch` and `probe` URLs, the probe key moving with the month, and
  `current_card` on the last day of a month taking the running month's card
  from the archive.
- `fetch_for_month` with the real archive replies: URLs built, render hook,
  a sheet the archive does not hold, a doubled-letter name, another month
  refused, the older template refused, transient failure raised, Brussels
  not asked.
