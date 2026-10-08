# EnergyVision and Brusol

Module: `custom_components/be_gas_prices/providers/energyvision.py`.
Tests: `tests/test_energyvision.py`, fixtures in
`tests/fixtures/energyvision/` (Brusol under `brusol/`).

## Where the cards are

- EnergyVision (Flanders, Wallonia): https://www.energyvision.be/nl-be/tariefkaart
  links `/sites/default/files/inline-files/EV-<MMYY>-<CODE>-<nl|WAL-fr>[_N].pdf`.
  `_N` is Drupal's re-upload suffix, so the current card is read off the
  listing. Probe: the listing's ETag.
- Brusol (Brussels): https://www.brusol.be/nl/elektriciteit-en-gas/schrijf-je-in-voor-goedkope-stroom-van-brusol
  links `https://www.brusol.be/sites/default/files/<upload YYYY-MM>/EV-<MMYY>-GSG-BXL-nl.pdf`,
  from October 2026 with a space before the closing quote. The page sends no ETag or Last-Modified, so Brussels has no probe key.

Gas is sold only with an electricity contract ("Fossiel gas mogelijk: in
combinatie"). Brusol's Goedkope Stroom is "exclusief voorbehouden aan
eigenaars van Brusol-zonnepanelen".

## Products and regions

| Contract id | Label | Code | Kind | Regions |
|---|---|---|---|---|
| `energyvision_gas` | EnergyVision Goedkope stroom - Gas | GSG | indexed (monthly ZTP-RLP-M) | Flanders, Wallonia, Brussels |
| `energyvision_gas_1_jaar_vast` | EnergyVision Goedkope stroom 1 jaar vast - Gas | GS1JVG | fixed, one year | Flanders, Wallonia |

Brusol has no GS1JVG card: its page links none and
`EV-0926-GS1JVG-BXL-nl.pdf` is 404 in both upload folders.

## How each figure is read

Reader: pdfplumber layout (`fetch_pdf_text_layout`). One regex set covers
the Dutch and the French cards.

- Card month: "Tariefkaart `<maand> <jaar>`" or "Carte tarifaire
  `<mois> <année>`". The code the card prints, "(EV-0926-GSG-nl)", is not
  used: it has typos ("GS1JVGK", "WAL-f").
- VAT: "inclusief 6% BTW" or "TVA à 6 %". Mandatory: the formula is grossed
  by it.
- Energy: "Fossiel gas – variabel 7,20€cent/kWh", "Fossiel gas – vast",
  "Gaz fossile – variable", "Gaz fossile – fixe" (Brusol prints a hyphen).
  The word must match the contract's kind, which refuses the other
  product's card.
- Fee: "Vaste vergoeding 50 €/jaar" or "Frais fixes 50 €/an".
- Formula: "1,02 x ZTP-RLP-M + 4,5 EUR/MWh", excluding VAT. The card only
  says its prices include 6%; the parameter document says "Op de
  tariefkaart staan de formules steeds weergegeven exclusief btw", and the
  arithmetic agrees. `factor = 1,02 x 1,06 / 1000`, `base = 4,5 x 1,06 / 1000`.
- The printed price is not the formula at the last known value but at the
  VNR yearly estimate (card note "berekend volgens methodologie Vlaamse
  Nutsregulator"): 7,20 for September 2026 is the formula at 62,209, the
  estimate beside August's realised 61,822 in the parameter document.
  At 61,822 the formula gives 7,16.
- DSO tables, each in its own column order:
  - Flanders, after "Vlaams Gewest": per tier "Vaste vergoeding €/jaar"
    then "Afname €cent/kWh", then databeheer and transport:
    `FLUVIUS ANTWERPEN 15,68 2,25641 83,22 0,90569 562,63 0,58608 18,92 0,165`.
  - Wallonia, after "la Région Wallonne": per tier "Consommation" then
    "Terme fixe", then transport, no metering:
    `TECTEO RESA 4,63 34,59 2,52 122,05 2,24 962,74 0,165`. Labels
    BRABANT WALLON, HAINAUT GAZ, ORES LUXEMBOURG, MOUSCRON and NAMUR are
    all ORES and must agree.
  - Brussels, after "Transport- en distributiekosten": per tier "Afname"
    then "Vaste vergoeding", then transport and the metering fee last:
    `SIBELGA 1,99 15,90 1,45 43,07 0,81 1.001,55 0,165 24,96`.
- Excise: "Verbruik tussen 0 & 12.000 kWh" / "Consommation entre 0 &
  12.000 kWh" and the next line ("Verbruik boven 12.000 kWh", "Verbruik
  tussen 12.000 & 150.000 kWh", "Consommation au-dessus de 12 000 kWh" and
  other wordings over the months). Failing that, "Accise spéciale x,xxx
  €cent/kWh" as a single open band.
- Energy contribution: "Energiebijdrage" or "Contribution énergétique",
  optional (dropped since August 2026).
- Walloon connection fee: "Redevance de raccordement", mandatory.
- Brussels levy: the rows after "openbaredienstverplichtingen" that start
  with "<=", "Tussen" or ">" and name m3/h; the amount is the last figure,
  since the two smallest rows carry their footnote digit inline
  ("<= 10 m3/h 2 3,56").

## The index

https://www.energyvision.be/nl-be/indexatieparameters links
`inline-files/EV-<MMYY>-Indexatieparameters-nl.pdf`. Page 5, "Fossiel gas /
ZTP-RLP-M (EUR/MWh) / Vlaamse Nutsregulator jaarschatting* (EUR/MWh)", has
one row per month: `Augustus 2026 61,822 62,209`. The read starts at
"ZTP-RLP-M (EUR/MWh)" and stops at the page footer ("EnergyVision NV"),
because the electricity tables before it have rows of the same shape. Rows
need both figures; "/ /" (before September 2024) and empty future rows are
skipped. "Maart 2026 50.358" prints dots. pypdf scrambles the 2026 rows, so
the layout reader is used here too. Index name: `ZTP-RLP-M`.

## Archive

- EnergyVision: the plain name `inline-files/EV-<MMYY>-<CODE>-<token>.pdf`
  resolves for every month from November 2025 except January 2026 for
  GSG.
  Where a month was uploaded again the plain name is the first upload.
- Brusol: the delivery-month folder is tried first, then the month before.
  August 2026 is in both, and the later upload (2026-08) carries the new
  excise; every other month from January 2026 is in exactly one, January
  in the 2025-12 folder. September to December 2025 are in neither.
- The history pages (`/nl-be/historiek-tariefkaarten-gas`,
  `/nl-be/historiek-tariefkaarten-GS1JVG`) are not used. They link a
  GS1JVG card as "GSG 12/2025", a 404 for "EBM gas 03/2026", and for
  August 2026 in Wallonia the `_0` re-upload that has no connection fee.

A card that does not parse or names another month moves on to the next
candidate; a transient failure raises. Readable months: January 2026 on
in Brussels, February 2026 on in Flanders and Wallonia, less the cards
refused below.

## Card errors, read as printed

- Walloon connection fee from the September 2026 cards on, both products,
  still printed in October: **0,07500 €cent/kWh, ten times the regulated
  0,0075** (CWaPE). Engie's card prints 0,00750, the supplier survey found
  0,0075 on Bolt, Mega, OCTA+, Luminus, Eneco and Ecofix too, and
  EnergyVision's own Walloon cards printed 0,00750 from November 2025 to
  August 2026. The Walloon order of 19 June 2003 (art. 2) sets the gas fee
  at 0,000075 EUR/kWh below 1 GWh a year and the low-voltage electricity one
  at 0,00075: the card prints the electricity rate. It is read as printed
  and billed at the law's 0,000075 EUR/kWh for the months `const.py` knows
  the law for (`_resolve.resolve_connection_fee`).
- Walloon GS1JVG from August 2026, still printed in October: **"Accise
  spéciale 4,876 €cent/kWh"**, the residential electricity excise since 1
  August 2026, in place of the gas bands. The law override in `_resolve.py`
  bills the gas rate for the months it covers. The August card also has no
  connection fee row and is refused.
- The August 2026 Walloon GSG re-upload (`_0`, the one the history page
  links) has no connection fee row and is refused; the plain name prints
  0,00750 with the old excise and contribution.
- Walloon DSO terms are cut to two decimals: ORES 4,28 / 2,20 / 1,63 against
  the CWaPE 4,28944 / 2,20574 / 1,63882, RESA 4,63 / 2,52 against 4,63912 /
  2,52946.
- TECTEO RESA prints 5,25 / 3,14 / 2,86 on the February, May and June 2026
  Walloon cards against 4,63 / 2,52 / 2,24 on the others.
- Sibelga metering 24,96 where the regulator and Engie give 24,95.
- The high excise band on the April to July 2026 cards is 0,96229, the
  first-quarter rate (all but the April GS1JVG Flemish card below). The
  law override bills each quarter's rate.
- The Flemish GS1JVG card of April 2026 prints the August excise (1,09286 /
  1,18296) and no energy contribution.
- The Walloon cards of November 2025 to January 2026 print the high excise
  band as 0,87717 where the Flemish ones print 0,94757; not checked which
  is right. Those cards are refused anyway (no transport column).
- EnergyVision's cards before February 2026 print no transport term and
  are refused. Brusol's January 2026 card prints it (0,165) and is read.
- The Walloon GSG card of December 2025 and of February 2026 also exists
  in Dutch (`WAL-nl`). Only the French February card is read; the French
  December card has no transport column either.
- The parameter document calls the formula "uitgedrukt in €cent/kWh"; the
  cards print it in EUR/MWh, which is what reproduces their prices.

Not modelled: the one-off welcome credit the Flemish cards print
("Eenmalige welkomstkorting 50 €" on GSG, 100 € on GS1JVG in September
2026); the Walloon and Brussels cards print no amount.

## What the tests pin

- GSG Flanders: price, fee, factor, base, the formula check at the VNR
  estimate 62,209 and at the realised 61,822, month, VAT.
- GS1JVG Flanders and Wallonia as fixed.
- Flanders: Antwerpen row in full, excise, no levy or fee.
- Wallonia: the reversed column order, ORES and RESA rows, the 0,00075
  connection fee as printed and billed at the law's 0,000075, the single
  4,876 excise on GS1JVG.
- Brussels: the Sibelga row with metering last, the nine levy amounts.
- August 2026: Brusol's first upload with the old levies, the Walloon plain
  name with the regulated fee, the `_0` re-upload refused.
- The other product's card and another region refused.
- `fetch` for four product and region pairs off the two pages.
- `fetch_for_month`: the plain name, both Brusol folders in order, another
  month, a transient failure, no Brussels GS1JVG.
- The index document table and `fetch_index` following the page link.
