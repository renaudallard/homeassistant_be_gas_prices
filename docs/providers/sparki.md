# Sparki

Module: `custom_components/be_gas_prices/providers/sparki.py`. Tests:
`tests/test_sparki.py`, fixtures in `tests/fixtures/sparki/`.

## Where the cards are

- Listing: https://sparki.be/elektriciteit-gas/tariefkaarten/, newest month
  first, linking every card as a WordPress upload:
  `https://sparki.be/wp-content/uploads/<YYYY>/<MM>/Sparki_Tariefkaart_<month>_Particulier_<SelfService|AtYourService>_Gas_<NL|FR>.pdf`.
- `<month>` is the Dutch month name on both languages, with no year. The
  upload folder is not the price month (April to June 2026 were uploaded in
  2026/07), so the card is found on the listing (`listed_cards`) and dated by
  its own text.
- `fetch` takes the first link for the product and language.
- No probe: the current card's URL is only known from the listing.

## Products and regions

| Contract id | Label | File | Fee €/year (September 2026) |
|---|---|---|---|
| `sparki_self_service` | Sparki Self Service | SelfService | 21,20 |
| `sparki_at_your_service` | Sparki At Your Service | AtYourService | 89,04 |

- The NL card is for Flanders ("particulieren in Vlaanderen"), the FR card
  for Wallonia ("particuliers en Wallonie"). No Brussels card.
- At Your Service XL is on the site but has no gas card on the listing.
- The card's validity sentence names its product ("geldig voor het product
  “Self Service”", "valable pour le produit “Self Service”") and region; a
  card naming another is refused.

## Why VariableRates

The formula "((0,105*TTF)+0,8)*1,06" (c€/kWh, VAT inclusive) never says which
TTF it means, and Sparki publishes no index values. Checked on 29 September
2026:

- the card defines nothing beyond the formula;
- the B2C general conditions
  (`2026-03-18_Sparki_AlgemeneVoorwaarden_B2C_NL.pdf`, article 4) defer to
  "Bijzondere Voorwaarden en de toepasselijke tariefkaart";
- the help pages say "De energieprijs wordt bepaald volgens de formule op je
  tariefkaart";
- the WordPress media library has nothing for "index", "indexatie",
  "parameter" or "TTF", and no page or post mentions TTF.

The printed "Geschatte maandprijs" is not a settled month either. The NL card
says "Weergegeven prijs gebaseerd op de VNR methodologie 6,24 c€/kWh":
(0,105 x 62,4 + 0,8) x 1,06 = 7,79, the printed price, and 62,4 EUR/MWh is
the value the Energy Together cards' "*" twelve-month VNR estimate implies
for September (HOA Nova: 7,74 = (62,4 + 15) / 10). TTF only reproduces the
price in EUR/MWh. So the energy leg is `VariableRates(price, fee, formula)`,
the price as printed.

## How each figure is read (pdfplumber layout text)

pypdf lists the DSO labels apart from their rows; pdfplumber keeps each row
on one line, so the card is read with `fetch_pdf_text_layout` (1,7 s to
extract on a Raspberry Pi, 2 to 3,4 s live with the listing).

- Month: "Tariefkaart september 2026" / "Carte tarifaire septembre 2026".
- Energy: "21,20 7,79 Geschatte maandprijs" / "21,20 7,79 Prix mensuel
  estimé": the yearly fee, then the price in c€/kWh.
- Formula: "berekend volgens de formule ((0,105*TTF)+0,8)*1,06." (kept for
  diagnostics).
- VAT: "inclusief 6% BTW" / "TVA 6% comprise" into `card_vat_rate`.
- Taxes, c€/kWh: "Bijdrage op de Energie (c€/kWh) 0,10577" / "Cotisation sur
  l’énergie (c€/kWh) 0,10577", "Verbruik tussen 0 & 12.000 kWh 0,87338" /
  "Consommation entre 0 & 12.000 kWh", "Verbruik > 12.000 kWh 0.9864" (a
  decimal point). Read as printed.
- Walloon connection fee: "Redevance raccordement 0,00750", mandatory on the
  FR card.
- DSO table: per tier "Vaste vergoeding €/jaar" then "Afname c€/kWh" for
  <= 5 000, 5 001 - 150 000 and >= 150 001 kWh, then "Tarief databeheer" and
  "Transport c€/kWh" (NL); the FR card has no databeheer column. Labels
  `FLUVIUS_LABELS`; ORES's five sub-areas (must agree) and "TECTEO - RESA".
  Example: "Fluvius Antwerpen 15,68 2,257 83,22 0,905 562,63 0,586 18,92
  0,165".

## Archive

- The listing keeps every month since April 2026. `fetch_for_month` reads
  each link named after the month, newest first, and returns the first card
  dated that month, reading past one it cannot read (another year's card of
  the same name); a month ahead of today (Home Assistant's clock) is not
  asked for, a transient failure raises.

## Known quirks and card errors

- Stale taxes: 0,87338 / 0.9864 and contribution 0,10577. 0.9864 is the
  law's April to June 2026 rate above 12 000 kWh (0,98645 VAT inclusive) to
  four decimals; 0,87338 is not the law's 0,87238. The law override corrects
  August 2026 onwards.
- The NL card says "Onbalanskosten en transportkosten voor gas zijn in de
  prijs inbegrepen" and still prints the Fluxys transport rate in its
  network table. The table is read as printed; whether transport is billed
  twice is not verified.
- pdfplumber garbles the rotated table header.

## Tests

`tests/test_sparki.py`: the price kept as printed, the VNR arithmetic from
the card's own figures, At Your Service's fee, the Flanders table and levies
(Antwerpen and Zenne-Dijle rows), the Walloon card (ORES, RESA, connection
fee), product and region refusals, a Walloon card without its connection fee,
the listing order, `fetch`, and `fetch_for_month` (August by name, past
another year's unreadable card, another month, before the listing,
transient, future).
