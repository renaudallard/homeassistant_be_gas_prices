# Contributor documentation

This directory documents the internals of the **Belgian Gas Prices** Home
Assistant integration (domain `be_gas_prices`), for people who maintain or
extend it. End-user setup lives in the [project README](../README.md).

| Document | Covers |
| --- | --- |
| [architecture.md](architecture.md) | The big picture, the module map, the data flow, adding a supplier |
| [glossary.md](glossary.md) | Belgian gas and Home Assistant terms |
| [pricing-model.md](pricing-model.md) | How a month of gas is billed |
| [coordinator.md](coordinator.md) | The refresh cycle, what is kept, staleness, services |
| [config-flow.md](config-flow.md) | The setup wizard and the options menu |
| [data-sources.md](data-sources.md) | Index values, calorific values, the card archive, the recorder, postcodes |
| [provider-framework.md](provider-framework.md) | The extractor protocol, the dataclasses, the shared readers |
| [entities.md](entities.md) | Sensors, button, services, Repairs, diagnostics |
| [ci-and-testing.md](ci-and-testing.md) | The tests, the live check, the card archive and the workflows |

One page per supplier lives under [providers/](providers/): where its cards
are, how each figure is read, its index, its archive and its quirks.

## A note on prices

No supplier price is typed into the source, and these pages follow the same
rule: any figure quoted is a regulated one the code carries with its window,
or one read off a real card in the test fixtures, and says so.
