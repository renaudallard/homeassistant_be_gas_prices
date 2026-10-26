# Social tariff (CREG)

Module: `custom_components/be_gas_prices/providers/social.py`. Tests:
`tests/test_social.py`, fixtures in `tests/fixtures/social/`. Asked for in
issue #6.

## What it is

A household granted protected status pays the social tariff whoever supplies
it: one price per kWh for the whole country, which the CREG sets each quarter
under the ministerial decree of 30 March 2007. The entry is the household's
contract, whoever delivers it: Engie, Luminus, another supplier or the
network operator as social supplier. It is never ranked or quoted against,
since only a protected household can be on it, and it has no signing figures
to type.

## Where the cards are

- One PDF per quarter, in French:
  `https://www.creg.be/sites/default/files/assets/Tarifs/Social/G-TSS-FR-<year>-Q<n>.pdf`
  (`G-TSS-NL-...` is the Dutch one, with the same figures).
- Q4 2022 is the oldest the CREG still serves (Q3 2022 answers 404), which is
  `FIRST_QUARTER`: a month before it has no card.
- `fetch` reads the running month's quarter, `fetch_for_month` a past
  month's. A card naming another quarter is refused, and a network failure
  is raised so the month cache asks again.
- No probe: the card is read again once its 24 hours are up.

## How the card is read (pypdf text)

```
Composante énergie (€cent/kWh) 4,750 5,035
Composante distribution (€cent/kWh) 0,832 0,882
Composante transport (€cent/kWh) 0,156 0,165
Total (€cent/kWh)* 5,738 6,082
```

Ex-VAT then with 6 % VAT ("TVA 6% comprise"), and the quarter as "4e
trimestre 2026". The four rows have to be there in that order, add up to the
total on both sides of VAT, and each VAT-inclusive figure has to be its
ex-VAT one at the card's rate, within the rounding the card prints; anything
else is refused. Every quarter from Q4 2022 to Q4 2026 reads (checked on
26 October 2026).

## What is billed

- **Energy**: the VAT-inclusive total less the distribution and transport
  components, so the household is billed the total the CREG recommends
  printing, not three rows rounded on their own. `VariableRates`, one price
  for every month of the quarter.
- **Network**: the distribution component as the proportional term of every
  tier and every operator of the region, and the transport component. It
  replaces the operator's tariffs: no fixed term, no Fluvius data management
  fee (`_resolve.resolve_for_delivery` leaves the social card's network as
  it is), no Brussels levy.
- **Excise**: none printed. The resolver bills the protected residential
  customer's rate on every kWh (`_resolve.law_protected_excise`): article
  419 on Justel from the day its wording dates (3,09 EUR/MWh from 1 August
  2026, 3,42 from 2027), else FPS Finance's tariff (2,77 EUR/MWh from July
  2023 to July 2026). A month neither gives cannot be priced.
- **Energy contribution**: none, the law exempts protected customers ("Les
  clients protégés sont exonérés de la cotisation énergie").
- **Walloon connection fee**: the law's, as on any Walloon card.

## Checked against the suppliers' social cards

Engie's social gas cards (`G_SOCIAL_R_GREY_C_F_00_<V|W|B>_F` on its pricing
document service) and Luminus's ("Tarif social Gaz" in its price list) for
October 2026 print 6,082 c€/kWh with no fixed fee, a special excise of
0,32754 c€/kWh (3,09 EUR/MWh with VAT) and, in Wallonia only, the connection
fee of 0,0075 c€/kWh. They print no Brussels levy and no data management fee.

## Card archive and live check

The archive files one row per region and month, labelled with the month it
bills. The live check fetches each region's card and checks its month, but
leaves its figures out of the fleet's consensus: its network part is the
CREG's, not an operator's.
