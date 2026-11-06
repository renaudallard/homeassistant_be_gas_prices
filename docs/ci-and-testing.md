# CI and testing

## The test suite

```bash
pip install -r requirements-dev.txt
pytest tests/ -q -n auto --dist loadfile
```

`--dist loadfile` keeps a file's tests in one worker, so a card read by
several tests of one file is rendered once (`tests.fixture_text` is cached per
process).

Reading the fixture cards is most of what the suite spends, a Frank or Bolt
card 40 to 50 seconds of pdfplumber on a Raspberry Pi, so `fixture_text`
also keeps each card's text on disk under `tmp/fixture_text/`. A directory
per reader code, named by a digest of `providers/_pdf_render.py`, the bounds it
reads under (`_pdf_bound.py`, `_pdf_bound_pdfminer.py`) and the pypdf and
pdfplumber versions, holds one file per card digest and reader, so a changed
card, reader or version is read afresh. `BE_FIXTURE_TEXT_CACHE` moves the
directory. On a Raspberry Pi the whole suite took 630 seconds with an empty
cache and 54 seconds with a full one.

### Layout

- `tests/test_<supplier>.py`: one per extractor module, against real cards
  under `tests/fixtures/<supplier>/`. `test_energy_together.py` covers the six
  brands on one template, `test_belvus.py` the Belvus copy of it.
- `test_pricing.py`, `test_running_costs.py`: the bill of a month, the tier,
  the excise slices, the year and the rolling year.
- `test_parity_features.py`: the signing card, typed rates, the custom
  supplier, earlier contracts, the comparison and the daily ranking.
- `test_config_flow.py`, `test_ha_setup.py`: the wizard and options menu, and
  a full setup through Home Assistant's loader with the sensors it creates.
- `test_markdown_text.py`: `escape_markdown` leaves nothing in a fetch error
  that markdown could render, and keeps it reading as given; the Repairs cards
  quoting it are checked in `test_ha_setup.py`.
- `test_postcodes.py`: the postcode table. `test_refresh_postcodes.py`: the
  script that generates it writes nothing when bpost or Synergrid comes back
  short or keeps failing.
- `test_calorific.py`: Atrias's monthly calorific value files, and its two
  hosts held to the rules the shared helpers keep.
- `test_excise_law.py`: article 419 read from Justel's page as served,
  refused when it is not the article, and the daily read and its backoff.
  `test_tarbel.py`: FPS Finance's excise tariff as served, the quarters
  `const.py` types against it, refused when it is not annex 7, and the
  daily read, its backoff and the Store. `conftest.py` holds the article
  and the tariff as published for every other test.
- `test_fetch.py`: the shared HTTP helpers against a local server
  (`socket_enabled`, since the harness blocks sockets otherwise): the caps,
  an address off https or on the household's network never asked, and a
  redirect refused before its target is asked, or followed over https to
  another site. `tests/local_https.py` serves a test over https
  under names of its own, on a certificate authority made for the run, and
  stands an http server in for an appliance on the household's network,
  logging what it is asked.
- `test_module_size.py`: every integration module under 1000 lines, the
  generated postcode table aside; a module past it is split along a seam it
  already has.
- `test_live_check.py`, `test_archive_cards.py`: the two daily scripts, with
  no network. Both also run steps of their workflows and hold each job of
  those to the tokens it may see.
- `test_card_finder.py`: the live check's search for a card that moved,
  replayed on November 2026's moves with the real pages and cards.
- `test_moved.py`: `providers/_moved.py`, the registry reading a moved card
  where its supplier lists it, on the same moves, and never at an address
  its page links on another host.
- `test_file_ci_issue.py`: `scripts/file_ci_issue.sh` against a fake `gh`
  that answers from files and logs its calls.
- `test_autorelease.py`: the release job of `autorelease.yml`, its own
  steps run under `bash -e` against a scratch origin and a fake `gh`: the
  retries, a re-run over a pushed tag, and a draft left by a failed publish.
- `tests/recorder/`: tests that need a real recorder database (the gas
  meter's statistics). `recorder_mock` must build its database before `hass`
  exists, so the directory's own `conftest.py` turns the parent's two autouse
  fixtures into no-ops and each test asks for what it needs.

### The fixture-driven pattern

A provider test reads a real card with the reader its extractor uses
(`fixture_text(supplier, name, "plain" | "layout" | "rows")`) and pins
what the card prints: the energy leg, and for an indexed product the
formula check (the formula at the index value the card names reproduces its
printed price); one full DSO row per region; the levies; each card error
the module knows of.
`fetch`, `fetch_for_month` and `fetch_index` are tested with the fetch
helpers patched on the provider module, so no test touches the network. A
figure the tests quote comes off a fixture card, never from memory.

`tests.approx` is `pytest.approx` typed as the float it stands in for, for a
dataclass built to compare against (`FixedRates(price=approx(0.0806), ...)`).

### Europe/Brussels

`tests/conftest.py` pins every test to Europe/Brussels. The `hass` fixture
defaults to US/Pacific, which would hide every month boundary and archive
offset bug a Belgian integration can have.

### Slow cards

pdfplumber takes 20 to 60 seconds a page on some cards on a Raspberry Pi
(Bolt, Frank), so `test_bolt.py` and `test_frank.py` raise the per-test
timeout to 180 seconds. The full suite takes 4 to 10 minutes on a Raspberry Pi
with three workers (`-n 3`) while the text cache is empty, under a minute once
it is full.

### Linting and typing

```bash
ruff check .
ruff format --check .
mypy --strict --cache-dir .mypy_cache_strict custom_components/be_gas_prices
mypy --cache-dir .mypy_cache_all tests/ scripts/
```

The tests and scripts are checked without `--strict`: the test harness has
untyped helpers the integration never imports. Each run keeps its own
cache: they check with different options, and through one shared cache each
threw the other's away, about two minutes a run on a Raspberry Pi against a
second or two once warm.

### The gate

`scripts/gate.sh` runs every check above at once against a throwaway git
worktree of HEAD, so editing the tree meanwhile cannot reach it and what is
checked is what a push would publish: commit first, then gate.

```bash
GATE_PYTHON=/path/to/venv/bin/python scripts/gate.sh   # the whole suite
scripts/gate.sh tests/test_ebem.py                     # one file
```

pytest spreads over the cores and the other checks take one each. The text
cache and the two mypy caches are the main checkout's, so a gate after the
first reuses them: on a Raspberry Pi, every check and the whole suite took 69
seconds with the caches full. The interpreter is `GATE_PYTHON`, else
`.venv/bin/python`; actionlint runs over the workflows when its binary sits
at `tmp/actionlint`.

With `GATE_REMOTE=<host>` pytest runs on that host instead, on the same
commit shipped with `git archive`, and the other checks stay local. The host
needs a venv at `~/gas_gate/.venv`, which the gate refreshes with uv when
`requirements-dev.txt` changes, and keeps its text cache in
`~/gas_gate/fixture_text`. A host that does not answer, or drops mid-run,
gets the suite run locally instead.

## scripts/live_check.py

Fetches every registered (supplier, contract, region) with the extractor the
integration uses and checks:

- the card is for the current month, allowing the previous month in the first
  five days of a month; a card with no end date (`valid_until` None) applies
  until the supplier replaces it and is not checked;
- every DSO row of a residential card prices T2, the tier of most
  households (`T2 rows`): the parser drops a T2 whose rate is not below
  T1's, which leaves them unpriced and the fleet nothing to compare. Dots'
  Fluvius Limburg row, documented, is a notice until 2027
  (`_KNOWN_MISSING_T2`). T3, which only some cards print and which no
  residential household reaches, is not checked;
- every `fetch_index` reads, lists each index its cards are priced on, and
  has a recent value for it: the month before, or two months back in the
  first ten days of a month (TotalEnergies' history always two), four
  months back for a quarterly index. A publication already looked at that
  runs later still is a notice until its allowance lapses
  (`_KNOWN_INDEX_DELAYS`: TotalEnergies' TTF_M_RLP history, still the 17
  September 2026 upload in November, to 1 December), and fails from then on;
- every supplier's list of the products it sells (`discover`: Frank's terms
  page, Eneco's and Mega's card listings, OCTA+'s tariff page) names the
  contracts registered for it, a withdrawn one left out: a product no contract is fails `<supplier>/catalog: no new products`,
  a contract the list no longer names `no products gone`, and a list that
  does not read or names nothing `products listed`. Frank's Plus tier was
  found by hand five days after its first card, before the list was read;
- the regulated figures agree across the fleet, month by month: per DSO the
  T1 and T2 terms, transport and metering; the federal excise and energy
  contribution; the VAT rate a card states for its customers; the Walloon
  connection fee; the Brussels levy by caliber.

Before the check, the runner resolves names through nsec.arnor.org over
DNS-over-TLS: Fluvius serves its zone from its own two nameservers, which
never answer a GitHub runner, whichever resolver asks (the Azure one, 1.1.1.1
or 8.8.8.8), so `www.fluvius.be` never resolved there. If nsec.arnor.org
cannot resolve Fluvius and Engie, the runner's own resolver is put back.

A fetch is tried three times (10 and 30 second pauses, 300 seconds each). A
failure that `is_transient_fetch_error` or a timeout explains on every
attempt is TRANSIENT, reported and never filed. After three such fetches in
a row the supplier is given up on for the run: its remaining cards and its
index publication are reported as transient without being fetched, so a host
that never answers does not run the job into its timeout. Both scripts
render in a daemon thread of its own (`card_texts.in_daemon_thread`), so a render that
never returns is abandoned at its timeout without holding the run's exit. A card published as page
images (`CardNotReadableError`) is parsed on the archive's OCR reading of its
bytes; until the archive has read them, it is a notice, and the archive
files a card its OCR could not read.

How the cross-check decides:

- Two figures agree when they differ by at most one unit of the last digit
  the less precise one prints, never coarser than the second decimal of a
  c€ or a euro. "2,20" read back as 0.022 still counts as printed to the
  hundredth.
- The fleet's figure comes in two steps: the figures most voters agree
  with, then among them the one most voters print as it is or to fewer
  digits, taken among those two voters print as they are where there are
  any. A figure printed to fewer digits agrees with finer ones on both
  sides of it ("0,99" with 0,9891 and with a stale 0,9864 alike), so it is
  the fleet's figure only where no finer one is shared, and one voter's own
  digits (EBEM's 0,105764 for the 0,1057668 the others round) are not the
  fleet's figure over the others'. Where the fleet's figure is printed to
  fewer digits than figures it agrees with that disagree with each other,
  it cannot tell which is right, and that is a notice.
- A supplier's vote is the module that parses it, so the six Energy Together
  brands count once.
- A tie is a notice: two voters against two is not a verdict on either.
- The excise, the energy contribution and the Walloon connection fee are
  measured against the law too: the run reads article 419 from Justel first
  (`federal: gas excise law`, a failure when the page is not the article)
  and FPS Finance's excise tariff (`federal: gas excise tariff`, a failure
  when the workbook is not annex 7; `federal: protected excise for
  YYYY-MM`, which no card prints, fails when the two disagree on the
  month or neither gives it). The two are held against each other on the
  household excise and the energy contribution in every month within a year
  of the run that both give (`federal: household excise, the law against
  FPS Finance's tariff`, `federal: energy contribution, the law against FPS
  Finance's tariff`, a notice when no month is given by both): the tariff
  is billed first, so the fleet would only show a misread of the law once
  the tariff stops listing a month. A month whose fleet mostly prints
  another figure than the one billed fails as `federal: excise ... against
  the law`, `federal: energy contribution ... against the law` (zero from
  August 2026) or `wallonia: connection fee ... against the law`. So does a
  fleet mostly stating another VAT rate than the one a card stating none is
  billed on (`federal: VAT rate ... against the law`): the month's rate in
  the card archive's VAT table, which the run reads from the checkout it
  takes the texts from (`archive: VAT table`, a notice without a table and
  `VAT_RATE_REDUCED` then, a failure when it does not read), and on which
  the law's figures and the Fluvius sheet's fee are measured too;
  a card stating another rate than the rest is billed on it, so
  it fails as a departure, never a notice. Where the fleet's figure is
  printed to fewer digits and agrees with the billed one, a finer figure
  it agrees with that the billed one does not is a notice under the same
  label: one voter is no majority, but a stale billed figure would
  otherwise pass unseen.
- A departure the integration does not bill is a notice rather than a
  failure: the excise of a month the law read from Justel or `const.py`
  sets, the Walloon connection fee of any month since the order of 2003,
  the energy contribution of a month the tariff, the law or `const.py`
  gives, and a
  Fluvius data management fee a card leaves out in a year whose Fluvius
  tariff sheet was read. Past those the same departure fails.
- The run reads this year's Fluvius gas tariff sheet first too (`fluvius:
  <year> gas tariff sheet`, a failure when none is listed or it gives no
  fee), and a Fluvius area whose fleet mostly prints another metering fee
  than the sheet's fails as `... metering ... against the Fluvius sheet`.
  While the sheet does not answer (a transient check), a Fluvius row
  printing no fee is transient too: its fee is the sheet's, which the
  integration bills once read.
- It also reads this year's network sheet of every DSO a custom entry can
  take its network from, through the integration's readers
  (`read_network_sheets`): each Fluvius area's, Brugel's, and ORES's and
  RESA's CWaPE grid valid today, one row each (`fluvius: <year> gas network
  sheets`, `brugel: <year> gas network sheet`, `cwape: <year> gas network
  grids`), a sheet not listed or that does not read failing, the network
  transient. Then each sheet's T1, T2 and metering figures, on the month's
  VAT, are measured against what the fleet prints for the DSO
  (`against_sheets`), compared the way two cards are and only where the
  fleet has a majority: a misread sheet stays within every bound, and only
  the cards show it. One that disagrees fails as `<regulator>: <dso>
  <figure> for <month> against the sheet`. On 6 November 2026 all eleven
  read and agreed with the October cards.
- A card error the provider notes document (`_KNOWN_CARD_ERRORS`: Bolt's
  and Ecofix's ORES and RESA terms, Bolt's Brussels levy, EBEM's Kempen
  T1) is a notice while the card prints that value: it is billed as
  printed, but already known. Any other value for it fails, and so does
  that value once the allowance lapses: each carries its expiry and why it
  is allowed, and the ones listed now lapse with the 2026 tariff year.

`--texts DIR` points at the `gas/` directory of a `be_price_cards` checkout:
a card whose bytes the archive holds is served its stored text instead of
being rendered, which is most of a run's time. `--fingerprint FILE` receives
the failing labels.

The report's tables show each detail as one code span (`_code_span`): a
detail can quote what a supplier serves, an error about its card or an
address the card finder read off its pages, and the report is posted as an
issue, where GitHub renders no mention, link, image or issue reference inside
a code span. The detail loses its backticks, which would close the span, and
its CRs and LFs, which would end the table row, and its pipes keep their
escape (`test_a_detail_is_shown_as_one_code_span`). A row's label is shown
the same way, since it can carry the month a card names itself for, as the
card prints it (`test_a_label_is_shown_as_one_code_span`), and the failing
labels the issue leads with lose their backticks and CRs before each is put
in its span (`test_a_failing_check_is_listed_as_one_code_span`).

Exit codes: 0 all passed, 1 a persistent failure, 2 only transient ones, 3
the check itself crashed. A crash before the check runs (an import that
fails) exits 1 without writing the fingerprint, and the workflow reads that
as 3.

### Where a moved card went (`scripts/card_finder.py`)

Last of all, the live check asks the card finder about every row that failed
on a `fetch` or a `card month` check, on a `<supplier>/<contract>/<region>`
label, for a supplier the finder covers (`card_finder.annotate`). The finder
reads the pages the supplier itself lists its cards on, keeps the addresses
that name the product, downloads up to four and adds to the row's detail which
of them names the running month and where each came from. It only reports: no
row's status changes, and the fingerprint is the failing labels and so does
not move. The integration reads a moved card by itself only for Elegant,
Engie and Dots, and only the product's own card for the month
(`providers/_moved.py`, see [provider-framework.md](provider-framework.md)),
whose candidates the finder reports for those three; anything else it finds
waits for a person to adopt the address in the extractor. The live check
asks each supplier's own `fetch` (`inspect.unwrap`), so a card the
integration reads where it moved still fails here.

| Supplier | Where it looks | November 2026 |
| --- | --- | --- |
| Elegant | the listing's `/Current/<product>_Residential.pdf` links, matched on the archive key | the renamed `Flex Gas` files |
| Engie | the residential prices pages, French and Dutch, which name every document code; the same product under any term, else a sibling of its family | Empty House's `_36_` code for `_00_` |
| EnergyVision | the files its `tariefkaart-2` page links, then where its listing's script looks (`_month_urls`) | nothing for the one-year fixed card, which was late, not moved |
| Luminus | the price-list archive's product id for the month and for the month before, each asked for the month's card | the old id's early copy, titled November, reported as coming from October's id |
| Belvus | the cards its services page and its history page link for the month | the services page's November cards, the history page still ending earlier |
| Dots | the card its product page links or embeds, at its new address then its old one | `/api/tariefkaarten/dots-connect-gas-digital` on the rebuilt site |

A card naming the month is not necessarily the month's card, as Luminus's
early copy showed, which is why it only reports. The tests replay each case
on the real page and card of that month (`tests/test_card_finder.py`), and the
search is a command of its own: `python scripts/card_finder.py <supplier>
<contract> <region> [YYYY-MM]` prints each candidate, where it came from and
whether it names the month, and exits 0 when one does.

## scripts/archive_cards.py

Stores today's cards in the shared
[be_price_cards](https://github.com/renaudallard/be_price_cards) repository
under `gas/`:

- `cards/<supplier>/<contract>/<region>/<YYYY-MM>.json`: the snapshot as
  `snapshot_codec.snapshot_to_json` writes it, which is what
  `month_cards.fetch_archived_card` reads, plus `_seen_on`, `_sources` and
  `_via` (`live` or `archive`), which the reader ignores. A source the OCR
  engine read carries `"ocr"`, the engine version.
- `texts/<YYYY-MM>/<sha256>.txt`: the text every parse read.
- With `--pdfs DIR`, each new PDF to `DIR/gas-<YYYY-MM>/<sha256>.pdf`, which
  the workflow uploads as the assets of the release of that name;
  `pdfs.json` says which release holds each digest.
- `indices/<supplier>.json`: each `fetch_index` table, merged into what
  earlier runs kept.
- `vat.json`: per card month, the residential VAT rate the rows state
  (`taxes.card_vat_rate`) when three suppliers or more state one rate and
  none states another, which `vat_rates.py` reads; a month they disagree on
  is written as `disputed`, one stated by too few as `too_few`, and neither
  is billed on.
- `law.json`: per month from January 2025 up to the current one, what the
  integration bills on from the law as the run read it (`_write_law`), a
  month it no longer gives keeping what was written, which `law_archive.py`
  reads. The run reads the law with the live check's `check_excise_law` and
  `check_excise_tariff`, so a network failure is tried again after a pause
  and one that persists is printed, costing the cards nothing.
- `network.json`: per month of last year and this one up to the current
  month, and per DSO key, what each regulator's network sheet gives a custom
  entry (`_network_months`, `_write_network`): each tier's terms and the
  metering, excluding VAT, off the sheet of the month's own year, read with
  the integration's readers (`read_area_network`, `read_network`,
  `read_listing`, `read_grids`) and fetched as the live check fetches. A
  DSO's month read again is written again, one no longer read keeps what was
  written, and a month past the rows' retention goes with them, which
  `network_archive.py` reads.

A row is rewritten only when the parse changed, so a quiet day writes
nothing. Months older than `--keep-months` (36) are removed, with the texts
no row names any more.

When the parser sources, the PDF readers or the OCR engine change (their
digest is stamped in `parser.txt`), or with `--reparse`, every row is parsed
again from its stored texts: the clock pinned to its capture day with
freezegun, a session that refuses every request it cannot answer from the
row, the texts served through the memo and `render_through`. A HEAD answers
for each card the row read and for the uploads Drupal keeps from before it,
and 404 for any other name, so EnergyVision's fetch, which asks for the
names in turn, comes to the card the row read. A GET for a card the row
read, asked under another folder, answers 404 as the site did when the row
was read: Brusol files each card under the month it uploaded it and the
parse asks the delivery month's folder first. A card that arrived inside JSON (OCTA+'s archive) stores a
`{{card:<sha256>}}` reference and gets its bytes back from the release. A
row the parser now refuses, or reads as another month's, is removed, so no
installation bills on the old reading; one that asks for something it never
read, or whose contract the supplier withdrew, is left as it was.

A card the text readers refuse as page images is read with the OCR engine,
[ocr_price_cards](https://github.com/renaudallard/ocr_price_cards), from its
trusted text: a line on which it refused a mark is left out, so a mandatory
figure it could not read fails the parse rather than being guessed. The
engine is installed from a pinned commit of its main branch, with numpy and
pypdfium2 pinned beside it; its version and git commit, with
the versions of the pypdfium2, numpy and pdfplumber it reads with, are
recorded on each source it read, and a stored reading is served again only
to the same engine, so a new glyph library reads those cards again and no
other: today's when the walk downloads it, a closed month's from its kept
bytes in the replay the new version starts. A month the new engine reads
worse, so that the parse fails, is removed like any row the parser now
refuses. The engine needs Python 3.14, so the archive job runs on 3.14; the
test suite, like an installation, runs on 3.13. `--ocr-failures FILE` lists
the cards the engine could not read, or whose reading failed the parse; the
workflow files them under `archive-cards-ocr`, since the live check has no
reading of those bytes and only notes them. The workflow also runs
`ocr-price-cards unlearnt` on each card the run stored: a card published as
images still embeds the fonts of the text set over its picture, so a glyph
the library lacks shows before any card needs it read from pixels. The
fonts and characters are filed under `archive-cards-glyphs`, naming the
card to learn them from; a card the tool crashes on fails the step, which
files nothing and lets the run go on. Both lists come from the walk, which
runs third-party code on what the suppliers serve, so each is posted in a
code block no line of it can close: its CRs go and every line starts with
`- ` (`test_the_walk_s_lists_stay_in_their_code_block`).

`--backfill N` also mirrors the N closed months before this one, at most
`--keep-months`, from every supplier archive, for months not held yet. Each
card is filed under the month it names, as a live row is, since a card in
force over several months (Bolt's variable ones) names the first and a
replay refuses a row whose card names another month. A held month whose PDF
the manifest does not record, an upload that failed after its row was
pushed, is fetched again for its bytes alone and its row left as it was.
`--only` restricts to a supplier. `--index-only` fetches nothing and only
rewrites the coverage sheets from what is on disk, which the workflow does
after the upload so the sheets link the PDFs just kept. A supplier is given
up for the day after three network failures in a row.

`scripts/card_texts.py` is the render cache both scripts share: each PDF
source of a row names the pypdf and pdfplumber versions that rendered its
text and a digest of the render code (`providers/_pdf_render.py` and the bounds
in `_pdf_bound.py` and `_pdf_bound_pdfminer.py`, with those versions, the
digest the fixture text cache is named by), and a stored text
is served only to the same versions and code, so a reader upgrade or a
render fix renders every card afresh, in the archive and in the live check,
for as long as it takes. A replay renders such a card again from its kept
bytes rather than parsing the old text, so a render fix or a reader upgrade,
both in the replay stamp, also reaches the months the walk no longer
downloads. Rendering every kept card takes hours, so the workflow asks
`rerender_due` first and gives such a run's walk six hours instead of one;
the run of 5 November 2026, the first after the render digest moved to
`_pdf_render.py`, stopped at the hour and stored nothing. The
live check serves the archive's OCR readings as they are: it installs no
engine and checks the figures the archive read.

## scripts/refresh_postcodes.py

Rebuilds `postcode_map.py` from bpost's postcode list and Synergrid's "DNB in
uw gemeente" lookup, one gas DSO set per postcode, with the script's
`OVERRIDES` applied. It prints every Synergrid answer on stdout for review and
writes nothing when a source comes back short. Standard library only.

## scripts/file_ci_issue.sh

Opens or updates the one issue a job files for a problem, found by its label.
The body carries a fingerprint of what failed; while the open issue's latest
post carries the same fingerprint and is younger than the cooldown, nothing
is posted, so a lasting outage gets one comment per cooldown rather than one
a day, and a failure that changes shape is posted at once. Only the bot's own
posts count (gh names it `app/github-actions` as an issue's author and
`github-actions` as a comment's), since anyone can comment and a comment
repeating the last marker kept the same failure quiet for another week; and
only the last marker of a post, the one the script writes, since a report
can quote one.

## GitHub workflows

| Workflow | When | What |
| --- | --- | --- |
| `test.yml` | push to main, pull requests, called by `autorelease.yml` | ruff, `mypy --strict` on the integration, `mypy` on tests and scripts, the test suite; pip's downloads, the two mypy caches and the fixture text are kept between runs with `actions/cache` |
| `validate.yml` | push, pull requests, daily | HACS and hassfest |
| `live_check.yml` | daily, and on pull requests that touch a provider, the readers of the law, of FPS Finance's tariff or of the card archive's tables, or the check | `live_check.py --texts` against a read-only clone of be_price_cards, with a read-only `github.token`; on exit 1 one issue labelled `live-check`, fingerprinted on the failing labels and filed by a `file` job that alone holds the issue token and reads the report the check hands over |
| `archive_cards.yml` | daily at 05:53 UTC, before the live check | seven jobs, so no token shares a runner with third-party code, each passing what it made to the next as an artifact: `walk`, with no token, on Python 3.14 with the OCR engine, clones be_price_cards without credentials and archives into `gas/`; `keep` uploads the PDFs as `gas-<YYYY-MM>` releases with `BE_GAS_CARDS` and prunes those past 36 months, taking only a folder named `gas-YYYY-MM` and a file named by its SHA-256, the names the walk writes, and skipping anything else the artifact holds (`test_only_the_cards_the_walk_names_are_uploaded`); `index`, with no token, whenever the walk succeeded even if an upload failed, rewrites the sheets and writes the root and `gas/` READMEs; `push` lays them over a fresh clone and pushes with `BE_GAS_CARDS` through gh's credential helper (`gh auth git-credential`), on no command line and in no file, and a rebase retry; `token` warns 14 days before the token expires; `ocr` files the cards the OCR could not read (`archive-cards-ocr`) and the glyphs its library lacks (`archive-cards-glyphs`); `report` files an issue when a job failed. Only `ocr`, `token` and `report` get a `github.token` that writes issues |
| `autorelease.yml` | push to main that changes `manifest.json` | the test suite, HACS and hassfest, then the tag and the release |

Every action the workflows use is pinned to a commit, with the release or
the branch it was taken from in a comment beside it, and so is the OCR
engine the walk installs from git, with the numpy and pypdfium2 it reads
with: a tag or a branch is whatever its repository makes it next, and these
run beside the cards token and on the cards installations bill on
(`test_every_action_and_git_install_names_a_commit`). To move one, look up
the commit the new tag names (`gh api repos/OWNER/REPO/commits/TAG --jq
.sha`) and change the hash and its comment together. The pins stop at the
action: hacs runs the `ghcr.io/hacs/action:main` image and hassfest the
untagged `ghcr.io/home-assistant/hassfest`, which their repositories move,
so both jobs keep a read-only token.

### What the repositories need

- `BE_GAS_CARDS`: a fine-grained token with contents read and write on
  be_price_cards.
- The electricity, gas and water archive workflows all write
  be_price_cards's root README with the same text, listing the three
  namespaces, so none rewrites another's. Should one of them drift, the gas
  push keeps its own text on a conflict.
