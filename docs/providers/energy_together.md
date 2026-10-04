# Energy Together platform

HOA Energy, Servolt Energie, Evident Energie, Power2You, Prijspunten Energie
and Smappee Smiles are brands of one supplier, Energy Together bv (BE
0800.104.795, Bornem). They run on one Odoo module and print their gas cards
from one Excel template. Belvus printed its cards on a copy of the same
template until August 2026 and is read by the same reader (see
[belvus.md](belvus.md)).

- Brands: `custom_components/be_gas_prices/providers/energy_together.py`,
  one `SupplierExtractor` per brand in `EXTRACTORS`.
- Template reader: `providers/_energy_together.py` (`parse_card`, shared with
  `belvus.py`).
- Tests: `tests/test_energy_together.py`; fixtures in
  `tests/fixtures/<brand id>/` (`hoa_energy`, `servolt`, `evident`,
  `power2you`, `prijspunten`, `smappee_smiles`).

## Brands and products

Every product is Flanders only ("Tariefkaart geldig voor de levering van gas
aan residentiële klanten in Vlaanderen") and indexed monthly on TTF_RLP.

| Extractor id | Label | Host | Archive from |
|---|---|---|---|
| `hoa_energy` | HOA Energy | www.hoa.energy | February 2026 |
| `servolt` | Servolt Energie | www.servoltenergie.be | April 2025 |
| `evident` | Evident Energie | www.evidentenergie.be | December 2024 |
| `power2you` | Power2You | www.power2you.be | May 2024 |
| `prijspunten` | Prijspunten Energie | www.prijspuntenenergie.be | July 2026 |
| `smappee_smiles` | Smappee Smiles | www.smappeesmiles.be | October 2024 |

One contract per gas card, keyed on a product group (September 2026 figures):

| Contract id | Group | Card names it | Formula (excl. VAT) | Fee €/year |
|---|---|---|---|---|
| `hoa_energy_nova` | Nova | NOVA GAS | (TTF_RLP x 1) + €15/MWh | 96 |
| `hoa_energy_prime` | Prime | PRIME GAS | (TTF_RLP x 1) + €20/MWh | 96 |
| `hoa_energy_apex_online` | APEX Online | APEX Online GAS | (TTF_RLP x 1) + €4/MWh | 30 |
| `servolt_control` | Control | CONTROL GAS | (TTF_RLP x 1) + €10/MWh | 96 |
| `servolt_comfort` | Comfort | COMFORT GAS | (TTF_RLP x 1) + €15/MWh | 96 |
| `servolt_solar` | Servolt Solar | SERVOLT SOLAR GAS | (TTF_RLP x 1) + €8/MWh | 35 |
| `evident_flexi` | Flexi | (nothing) | (TTF_RLP x 1) + €13/MWh | 60 |
| `power2you_variabel` | Variabel | VARIABEL GAS | (TTF_RLP x 1) + €7/MWh | 60 |
| `power2you_flex` | Flex | FLEX GAS | (TTF_RLP x 1,05) + €10/MWh | 96 |
| `prijspunten_marktflex` | Marktflex | Marktflex Gas | (TTF_RLP x 1) + €12/MWh | 120 (90 in August) |
| `smappee_smiles_variabel` | Variabel | VARIABEL SMILES GAS | (TTF_RLP x 1) + €7/MWh | 60 |

The groups not listed link one of these cards: HOA's Volt (Nova) and Prime
Plus (Prime), Servolt's Dynamic Control (Control), Evident's Dynamisch
(Flexi), Power2You's Dynamic (Variabel) and Flow (Flex), Prijspunten's
Comfortflex (Marktflex), Smappee's Lite, Smart and Sunplus (Variabel Smiles).

## Where the cards are

- `https://<host>/products`: one block per product group, the name in an
  `<h3>`, then links
  `<a href="/web/content/xx.product.group.tariff.card/<id>/file">` with the
  card's name in the second `<span>` ("Tariefkaart_NOVA_NG"). The gas card is
  the one whose name ends in `_NG` or `_Gas` (`current_card_path`); a group
  with none or two is refused.
- The ids change every month, and groups sharing a card link different ids
  to byte-identical files. The names drift (Power2You: B2C_VAR_NG,
  B2C_VAR_GAS, Variabel_Gas; Evident: Variabel_Gas until March 2025), which
  is why a contract is keyed on its group.
- The card's printed product is checked against the contract (`product_key`
  strips spacing, case and the trailing "GAS"). Evident's card names none
  ("Tariefkaart particulier gas - september 2026"), so its expected name is
  empty and a card naming a product is refused.
- No probe: the ids change monthly and the listing is rendered per request.

## How each figure is read (pypdf text, `parse_card`)

- Month and product: "Particulier gas", the product, then "september 2026"
  (`card_month`). `publication_label` and `valid_until` come from it.
- Fee: "Jaarlijkse abonnementskost €96", VAT inclusive under "Alle prijzen
  zijn inclusief BTW tenzij anders vermeld" (the one-page APEX card omits the
  sentence).
- Two prices, both "excl. BTW": "*€c7,74/kWh" the VNR estimate for the next
  twelve months and "**€c7,67/kWh" the formula at the last known month
  ("laatst gekende waarde van TTF-DAM 8/2026: €61,72903/MWh"). `price` is the
  "**" figure x 1,06.
- Formula: "Formule (TTF_RLP x 1) + €15/MWh", excluding VAT, the index in
  EUR/MWh: factor = 1 / 1000 x 1,06, base = 15 / 1000 x 1,06 (EUR/kWh). The
  card states no VAT rate, so the gross-up uses `const.VAT_RATE_REDUCED` and
  `card_vat_rate` is None.
- Taxes: "Bijzondere accijns op Energie < 12.000 kWh €c0,8724", "> 12.000 kWh
  €c0,9457", "Bijdrage op Energie €c0,1057", VAT inclusive, read as printed.
  The two excise rates are read in order between the first label and the
  contribution, because the August Belvus layout prints both labels before
  both rates.
- "4. Databeheer - en transportkosten": the merged cell comes out as one
  line "18,92 0,165" (databeheer €/year, transport c€/kWh although the unit
  reads "€c/kW jaar"). Exactly one such line must exist.
- "5. Distributiekosten": "FLUVIUS ANTWERPEN 2,26 15,68 0,91 83,22", columns
  T1 Variabel (c€/kWh), T1 Vast (€/year), T2 Variabel, T2 Vast. No T3.
  Power2You prints the T2 term with three decimals.

## The Fluvius labels are misassigned

The template prints the eight labels in the order Antwerpen, Limburg, West,
Imewo, Midden-Vlaanderen, Kempen, Zenne-Dijle, Halle-Vilvoorde, but the
figures follow the alphabetical order every other card and the Flemish
regulator use. Checked row by row against the regulator's 2026 figures
(tmp/research/regulated/FINDINGS.md section 2) and Engie's September card:

| Printed label | Figures on the row | Whose they are |
|---|---|---|
| FLUVIUS ANTWERPEN | 2,26 15,68 0,91 83,22 | Antwerpen (15,68 + 2,25641, 83,22 + 0,90569) |
| FLUVIUS LIMBURG | 2,50 17,59 0,98 93,38 | Halle-Vilvoorde (17,59 + 2,49578, 93,38 + 0,98026) |
| FLUVIUS WEST | 2,63 18,61 1,02 98,81 | Imewo (18,61 + 2,62774, 98,81 + 1,02399) |
| FLUVIUS IMEWO | 2,27 16,17 0,88 85,83 | Kempen (16,17 + 2,27489, 85,83 + 0,88186) |
| FLUVIUS MIDDEN-VLAANDEREN | 2,24 14,59 0,98 77,46 | Limburg (14,59 + 2,24027, 77,46 + 0,98293) |
| FLUVIUS KEMPEN | 2,32 16,33 0,91 86,71 | Midden-Vlaanderen (16,33 + 2,31716, 86,71 + 0,90988) |
| FLUVIUS ZENNE-DIJLE | 2,69 19,03 1,06 101,02 | West (19,03 + 2,69459, 101,02 + 1,05500) |
| FLUVIUS HALLE-VILVOORDE | 2,60 18,40 1,02 97,72 | Zenne-Dijle (18,40 + 2,60252, 97,72 + 1,01649) |

`_dsos` therefore maps the rows by position (`_ROW_OWNERS`) and refuses the
card unless the labels read exactly `_PRINTED_LABELS`, in that order. If the
supplier corrects the labels, or moves the figures to follow them, the card
fails with "not the order this reader maps by position"; check the rows
against the regulator's figures again and map by label if they now agree.
The fixed terms alone tell the eight areas apart.

The 2025 table of the template (on every 2025 card, and on Belvus's January
to March 2026 ones) is laid out otherwise: each row carries its own label's
figures ("FLUVIUS WEST 2,48 17,15" is West's, as EBEM's labelled card of
December 2025 prints it), so read by position seven areas of eight would get
another area's tariff. It also carries the broken row "FLUVIUS LIMBURG 2,09
13,32 70,72 70,72", whose mid tier the shared `dso_overlay` drops as not
degressive. That row is what tells the 2025 table, and `_dsos` refuses a card
printing it: those months are months with no card.

## Index

- TTF_RLP: "RLP gewogen gemiddelde dagprijs op de TTF Day Ahead markt" for
  the delivery month, known only once the month is over, so `settled` is
  False and the card prices the month at the previous month's value.
- Publication: each brand's `/indexatieparameters` page links
  `/web/content/<id>` ("De indexatieparameters van de prijsformules vind je
  hier"), a PDF with the table "GAS: TTF_RLP (per maand)", "TTF_RLP (c€/kWh)",
  "MAAND 2025 2026", one row per month. `parse_index_publication` returns it
  in EUR/MWh (x 10); a month not known yet has no figure in the last column.
- The six brands link one publication, under an id that changes when it is
  replaced. Smappee's page once linked an id its host no longer served while
  the others linked the current one. `_fetch_index` tries the brand's own
  page first, then the other brands' pages, and when all fail raises a
  transient failure in preference to the brand's own.
- The publication has two decimals of c€/kWh where the cards' footnotes
  have three to five, and it is not consistent about rounding: December 2025
  to March 2026 are cut (the cards' 34,058 EUR/MWh for January is published
  as 3,40), June and July 2026 rounded (53,07468 as 5,31). Every month from
  January 2025 to August 2026 is within 0,1 EUR/MWh of the cards' value,
  about 0,0001 EUR/kWh at factor 1.

## Archive

- `https://<host>/products/history`: an `<h2>` per group, then rows of the
  card link and its month in a `<span>` ("september 2026"), newest first.
  `archived_card_path` takes the group's first gas row for the month; where a
  month has two rows (Power2You, June and October 2024) the first is the
  later upload.
- Checked live on 29 September 2026 for one product per brand: every card
  the archives list from January to September 2026 reads. The February to
  December 2025 cards are refused for their 2025 table (above), and the
  January 2025 ones checked do not parse (no card month, or no databeheer
  line). A refused month is a month with no card.
- A month ahead of today (Home Assistant's clock) is not asked for; a card
  naming another month is refused; a transient failure raises.

## Known quirks and card errors

- The misassigned labels and the 2025 table, above.
- Every card from February 2025 to September 2026 prints the same tax block:
  excise 0,8724 / 0,9457 c€/kWh and contribution 0,1057. From August 2026 the
  law override replaces them. Before that, 0,8724 is the law's 8,23 EUR/MWh
  with VAT, but 0,9457 matches none of the 2026 rates above 12 000 kWh
  (0,96229 in January to March, 0,98645 in April to June, 0,98914 in July,
  VAT inclusive).
- The July 2026 cards of HOA, Servolt, Evident, Power2You and Smappee state
  "TTF-DAM 6/2026: €46,94094/MWh", May's value; the publication has June at
  4,51 c€/kWh and Prijspunten's July card states 45,076. Their "**" price for
  July is therefore June's again. The index table reprices the month.
- Evident's August 2026 card reads "TTF-DAM 7/2036".
- Transport is labelled "€c/kW jaar"; the figure is c€/kWh.

## Tests

`tests/test_energy_together.py`:

- `test_energy_leg_is_the_formula_at_the_last_known_month`: every September
  card, price, fee, factor, base, and the formula at the footnote's value
  reproducing the "**" price.
- `test_rows_carry_the_alphabetical_areas_figures`: every card, all eight
  rows against the regulator's figures by position.
- `test_the_label_limburg_carries_halle_vilvoorde`,
  `test_power2you_prints_three_decimals`: individual rows.
- `test_levies_are_read_as_printed`, `test_august_card_and_evident_typo`.
- `test_a_card_with_the_2025_table_is_refused`: a December 2025 card, whose
  rows carry their own labels' figures.
- Refusals: another product's card, relabelled rows, a lost formula,
  Wallonia, an unknown contract.
- Discovery: `test_current_card_is_the_groups_gas_link` (every contract),
  `test_archive_is_keyed_on_the_group_through_name_drift`, `fetch`,
  `fetch_for_month` (URL, other month, before the archive, transient, future).
- Index: `test_index_publication_in_eur_per_mwh`,
  `test_index_falls_back_to_another_brands_page`,
  `test_index_failing_everywhere_raises_a_transient_error`.
