# Code reviewer memory

Recurring issues and project-specific patterns, one line each. Keep this file short and current.

- Marker checks must use `item.get_closest_marker(name)`, not `name in item.keywords` (keywords include node names: dirs, param ids); pytester tests should place fixtures where dir names can't mask the bug.
- import-linter `forbidden` contracts check indirect chains by default; layered "X must not import Y" rules where X legitimately reaches Y via services need `allow_indirect_imports = true`, plus a positive self-test of the legal chain.
