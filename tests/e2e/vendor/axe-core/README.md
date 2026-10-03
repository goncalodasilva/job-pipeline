# axe-core (vendored for accessibility tests)

- Version: 4.13.0
- Source: https://registry.npmjs.org/axe-core/-/axe-core-4.13.0.tgz
- npm integrity of the tarball: sha512-UzGt8zg7Ny8djbYMhxl2zuEevVa7r2gJjYY5Lwr1xM7+XU2nd6CkIWFTVcCIbAP63vSz71NaVyyuSk9lHKcy0A==
- SHA-256 of `axe.min.js`: c24f097bd2f451d4f933e8bc7d8d539f8672a2ebcb5cc9f9f3eec8ca9470a0c1
- License: MPL-2.0 (`LICENSE`); bundled third-party code under MIT (`LICENSE-3RD-PARTY.txt`).
- Used only by the end-to-end tests (ADR-022); never shipped with the app.

To update: download the new tarball from npm, check its integrity against the registry, copy
`axe.min.js` and both license files here, and update the version and hashes in this file
(the unit test reads the SHA-256 from here).
