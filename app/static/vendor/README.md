# Vendored front-end libraries

## htmx

- Version: 2.0.11
- Source: https://registry.npmjs.org/htmx.org/-/htmx.org-2.0.11.tgz (`dist/htmx.min.js`)
- npm integrity of the tarball: sha512-Thx/WtpeOQqSrqBCw/A1cwGJGg4UrVa3+sW0GmrM3p4gJgO89ecH4qtbnyzDDWFvBTqjnIMCgELTNt636dtamA==
- SHA-256 of `htmx.min.js`: d6fdc75f204e6bdefa99b69bf1e6d4ac69b8a364f77929f45c13476b4000f717
- License: 0BSD (`htmx.LICENSE`).
- Pinned by ADR-029 (htmx 2.0.x). The htmx SSE extension is not vendored: live updates use a small fetch-based reader instead (ADR-077).

To update: download the new tarball from npm, check its integrity against the registry, copy
`dist/htmx.min.js` and `LICENSE` (as `htmx.LICENSE`) here, and update the version and hashes
in this file (the integration test reads the version and SHA-256 from here).

## SortableJS

- Version: 1.15.7
- Source: https://registry.npmjs.org/sortablejs/-/sortablejs-1.15.7.tgz (`Sortable.min.js`)
- npm integrity of the tarball: sha512-Kk8wLQPlS+yi1ZEf48a4+fzHa4yxjC30M/Sr2AnQu+f/MPwvvX9XjZ6OWejiz8crBsLwSq8GHqaxaET7u6ux0A==
- SHA-256 of `Sortable.min.js`: bf4241bc73fef7f11c59a283a69fe8051cdd31c6d8ff5a2b9ba219e7831fcf76
- License: MIT (`Sortable.LICENSE`).
- ADR-102 approves 1.15.x. Not loaded on a page until M5.4; then board only.

To update: verify the tarball's SHA-512 against npm metadata before copying only
`Sortable.min.js` and `LICENSE`; update these pins and run the vendor and drag tests.
