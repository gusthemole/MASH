# docs/density-chain

The home for the **system density-chain** of MASH — a chain-of-density map of the
whole engine as a branching **"density-trellis"**: one shared trunk (the engine at
increasing density) plus one branch per subsystem class, each branch a fixed-length
five-tier chain whose tiers traverse **essence → current machinery → frontier /
limit**. It is the [chain-of-density method](https://github.com/OpenCnid/chain-of-density)
(Adams et al. 2023, [arXiv:2309.04269](https://arxiv.org/abs/2309.04269)) applied
to a codebase reverse-engineered from its code, rather than to a research paper.

## Contents

| File | What it is |
|---|---|
| [`DENSITY-CHAIN.md`](DENSITY-CHAIN.md) | The density-trellis of the MASH engine — trunk + 9 class branches + a cross-section. **Ground truth.** |
| [`DENSITY-CHAIN.html`](DENSITY-CHAIN.html) | A self-contained, theme-aware interactive render of the same trellis. **The map.** |

## Conventions

- **The markdown is ground truth; the HTML is a render of it.** If they disagree,
  the markdown wins — and both are subordinate to the code.
- **Orientation aid only, not authority.** Reverse-engineered from a read-only
  clone, July 21, 2026; it drifts as the engine changes.

## Provenance & attribution

MASH is **Matthew Murphy's (Mogura / Lexideck)** — [github.com/gusthemole/MASH](https://github.com/gusthemole/MASH),
**Artistic License 2.0**. This map lives in OpenCnid's fork with the original
`LICENSE` and attribution intact. It was produced because MASH is a strikingly
close *sibling design* to OpenCnid's Trellis engine; the full correspondence
analysis lives in the Trellis repo at `docs/architecture/SELF_DESCRIBING_SURFACES.md`.
Appreciation, not appropriation.
