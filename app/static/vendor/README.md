# Vendored front-end libraries

## htmx

- Version: 2.0.11
- Source: https://registry.npmjs.org/htmx.org/-/htmx.org-2.0.11.tgz (`dist/htmx.min.js`)
- npm integrity of the tarball: sha512-Thx/WtpeOQqSrqBCw/A1cwGJGg4UrVa3+sW0GmrM3p4gJgO89ecH4qtbnyzDDWFvBTqjnIMCgELTNt636dtamA==
- SHA-256 of `htmx.min.js`: d6fdc75f204e6bdefa99b69bf1e6d4ac69b8a364f77929f45c13476b4000f717
- License: 0BSD (`htmx.LICENSE`).
- Pinned by ADR-029 (htmx 2.0.x). The SSE extension is added in M2.

To update: download the new tarball from npm, check its integrity against the registry, copy
`dist/htmx.min.js` and `LICENSE` (as `htmx.LICENSE`) here, and update the version and hashes
in this file (the integration test reads the version and SHA-256 from here).
