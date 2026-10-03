# Code reviewer memory

Recurring issues and project-specific patterns, one line each. Keep this file short and current.

- Marker checks must use `item.get_closest_marker(name)`, not `name in item.keywords` (keywords include node names: dirs, param ids); pytester tests should place fixtures where dir names can't mask the bug.
