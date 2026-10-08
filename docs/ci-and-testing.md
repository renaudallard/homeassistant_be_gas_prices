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
per reader code, named by a digest of `providers/_pdf.py` and the pypdf and
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
- `test_postcodes.py`: the postcode table.
- `test_calorific.py`: Atrias's monthly calorific value files.
- `test_fetch.py`: the shared HTTP helpers against a local server
  (`socket_enabled`, since the harness blocks sockets otherwise).
- `test_live_check.py`, `test_archive_cards.py`: the two daily scripts, with
  no network.
- `test_file_ci_issue.py`: `scripts/file_ci_issue.sh` against a fake `gh`
  that answers from files and logs its calls.
- `tests/recorder/`: tests that need a real recorder database (the gas
  meter's statistics). `recorder_mock` must build its database before `hass`
  exists, so the directory's own `conftest.py` turns the parent's two autouse
  fixtures into no-ops and each test asks for what it needs.

### The fixture-driven pattern

A provider test reads a real card with the reader its extractor uses
(`fixture_text(supplier, name, "plain" | "layout")`) and pins what the card
prints: the energy leg, and for an indexed product the formula check (the
formula at the index value the card names reproduces its printed price); one
full DSO row per region; the levies; each card error the module knows of.
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

## scripts/live_check.py

Fetches every registered (supplier, contract, region) with the extractor the
integration uses and checks:

- the card is for the current month, allowing the previous month in the first
  five days of a month; a card with no end date (`valid_until` None) applies
  until the supplier replaces it and is not checked;
- every `fetch_index` reads, lists each index its cards are priced on, and
  has a recent value for it (two months, four for a quarterly index);
- the regulated figures agree across the fleet, month by month: per DSO the
  T1 and T2 terms, transport and metering; the federal excise and energy
  contribution; the Walloon connection fee; the Brussels levy by caliber.

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
- A supplier's vote is the module that parses it, so the six Energy Together
  brands count once.
- A tie is a notice: two voters against two is not a verdict on either.
- A departure the integration does not bill is a notice rather than a
  failure: a federal levy or the Walloon connection fee of a month
  `const.py` knows the law for, and a
  Fluvius data management fee a card leaves out in the tariff year
  `const.py` carries the regulated one for. Past those windows the same
  departure fails.
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

Exit codes: 0 all passed, 1 a persistent failure, 2 only transient ones, 3
the check itself crashed. A crash before the check runs (an import that
fails) exits 1 without writing the fingerprint, and the workflow reads that
as 3.

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

A row is rewritten only when the parse changed, so a quiet day writes
nothing. Months older than `--keep-months` (12) are removed, with the texts
no row names any more.

When the parser sources, the PDF readers or the OCR engine change (their
digest is stamped in `parser.txt`), or with `--reparse`, every row is parsed
again from its stored texts: the clock pinned to its capture day with
freezegun, a session that refuses every request, the texts served through
the memo and `render_through`. A card that arrived inside JSON (OCTA+'s archive) stores a
`{{card:<sha256>}}` reference and gets its bytes back from the release. A
row the parser now refuses, or reads as another month's, is removed, so no
installation bills on the old reading; one that asks for something it never
read, or whose contract the supplier withdrew, is left as it was.

A card the text readers refuse as page images is read with the OCR engine,
[ocr_price_cards](https://github.com/renaudallard/ocr_price_cards), from its
trusted text: a line on which it refused a mark is left out, so a mandatory
figure it could not read fails the parse rather than being guessed. The
engine is installed from its main branch; its version and git commit, with
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
files nothing and lets the run go on.

`--backfill N` also mirrors the N closed months before this one, at most
`--keep-months`, from every supplier archive, for months not held yet. Each
card is filed under the month it names, as a live row is, since a card in
force over several months (Bolt's variable ones) names the first and a
replay refuses a row whose card names another month.
`--only` restricts to a supplier. `--index-only` fetches nothing and only
rewrites the coverage sheets from what is on disk, which the workflow does
after the upload so the sheets link the PDFs just kept. A supplier is given
up for the day after three network failures in a row.

`scripts/card_texts.py` is the render cache both scripts share: each PDF
source of a row names the pypdf and pdfplumber versions that rendered its
text and a digest of the render code (`providers/_pdf.py` with those
versions, the digest the fixture text cache is named by), and a stored text
is served only to the same versions and code, so a reader upgrade or a
render fix renders every card afresh, in the archive and in the live check,
for as long as it takes. A replay renders such a card again from its kept
bytes rather than parsing the old text, so a render fix or a reader upgrade,
both in the replay stamp, also reaches the months the walk no longer
downloads. The
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
a day, and a failure that changes shape is posted at once.

## GitHub workflows

| Workflow | When | What |
| --- | --- | --- |
| `test.yml` | push to main, pull requests, called by `autorelease.yml` | ruff, `mypy --strict` on the integration, `mypy` on tests and scripts, the test suite; pip's downloads, the two mypy caches and the fixture text are kept between runs with `actions/cache` |
| `validate.yml` | push, pull requests, daily | HACS and hassfest |
| `live_check.yml` | daily, and on pull requests that touch a provider or the check | `live_check.py --texts` against a read-only clone of be_price_cards; on exit 1 one issue labelled `live-check`, fingerprinted on the failing labels |
| `archive_cards.yml` | daily at 05:53 UTC, before the live check | on Python 3.14 with the OCR engine, clones be_price_cards without credentials, archives into `gas/`, uploads the PDFs as `gas-<YYYY-MM>` releases and prunes those past 12 months, then, whenever the walk succeeded even if an upload failed, writes the root and `gas/` READMEs, pushes with `BE_GAS_CARDS` passed as a header, never stored, and a rebase retry, then warns 14 days before the token expires, files an issue when a step failed; files the cards the OCR could not read (`archive-cards-ocr`) and the glyphs its library lacks (`archive-cards-glyphs`) |
| `autorelease.yml` | push to main that changes `manifest.json` | the test suite, HACS and hassfest, then the tag and the release |

### What the repositories need

- `BE_GAS_CARDS`: a fine-grained token with contents read and write on
  be_price_cards.
- The electricity, gas and water archive workflows all write
  be_price_cards's root README with the same text, listing the three
  namespaces, so none rewrites another's. Should one of them drift, the gas
  push keeps its own text on a conflict.
