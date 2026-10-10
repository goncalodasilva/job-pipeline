# htmx patterns used in this app

## Base template: token, live region, scripts

```html
<!doctype html>
<html lang="en" data-theme="{{ theme }}">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <link rel="stylesheet" href="{{ static('css/app.css') }}">
  <script src="{{ static('vendor/htmx.min.js') }}" defer></script>
  <script src="{{ static('js/app.js') }}" defer></script>
</head>
<body hx-headers='{"X-JP-Token": "{{ launch_token }}"}'{% if live_updates %} data-events-url="/events"{% endif %}>
  <a class="skip-link" href="#main">Skip to content</a>
  {% include "fragments/shell/_sidebar.html" %}
  <main id="main" tabindex="-1">{% block content %}{% endblock %}</main>
  <div id="announcer" class="visually-hidden" aria-live="polite" aria-atomic="true"></div>
</body>
</html>
```

## Route returning a page or a fragment

```python
@router.get("/inbox")
async def inbox(request: Request, svc: InboxService = Depends(inbox_service)) -> HTMLResponse:
    view = await svc.queue_view(filters_from(request))
    return render(request, page="pages/inbox.html", fragment="fragments/inbox/_queue.html", ctx={"view": view})
```

`render()` picks the fragment when `request.headers.get("HX-Request") == "true"`.

## Action with focus and announcement

```html
<button hx-post="/jobs/{{ job.id }}/remove"
        hx-target="#focus-card" hx-swap="outerHTML">Remove · R</button>
```

The route responds with the next card (its heading carries `data-autofocus`) and the header `HX-Trigger: {"announce": "Job removed. Undo is available for a few seconds."}`. `app.js` handles both:

```js
document.body.addEventListener("announce", (e) => {
  document.getElementById("announcer").textContent = e.detail.value;
});
// Fragments mark the element that should receive focus with data-autofocus.
document.body.addEventListener("htmx:afterSettle", () => {
  const target = document.querySelector("[data-autofocus]");
  if (!target) return;
  target.removeAttribute("data-autofocus");
  if (!target.hasAttribute("tabindex")) target.setAttribute("tabindex", "-1");
  target.focus();
});
```

## Background task status on tasks:resync

`app.js` reads `/events` with fetch (no `EventSource`, no htmx SSE extension; ADR-077) and dispatches `tasks:resync` on `document.body` for each `resync` event. The server sends one first on every connection. Any element inside `<body>` can refresh on it:

```html
<div id="task-{{ task.id }}" hx-get="/tasks/{{ task.id }}" hx-trigger="tasks:resync from:body, tasks:updated from:body"
     hx-swap="outerHTML">{% include "fragments/tasks/_status.html" %}</div>
```

The GET carries the token from `<body hx-headers>` like any fragment request; the token is never in the URL. Each connection starts with a resync, so the task fragments re-fetch their state then. Reconnects and task-change events come with M2.18.

## Redirect from htmx requests

Server side: return `HX-Redirect: /setup` for htmx requests, a normal 303 redirect for page requests.

## Board drag-and-drop with a keyboard alternative

```html
<ul class="column" data-status="{{ status.id }}" data-sortable>
  {% for app in column %}
  <li class="card" data-id="{{ app.id }}">
    {{ app.company }} · {{ app.role }}
    <select name="status" aria-label="Move {{ app.company }} to"
            hx-post="/applications/{{ app.id }}/status" hx-trigger="change" hx-target="#board">
      {% for s in statuses %}<option value="{{ s.id }}" {% if s.id == app.status %}selected{% endif %}>{{ s.name }}</option>{% endfor %}
    </select>
  </li>
  {% endfor %}
</ul>
```

`app.js` initializes SortableJS on `[data-sortable]` and, on drop, posts to the same `/applications/<id>/status` endpoint, so both paths share one code path on the server.

## Prefetching the next inbox card

The queue fragment includes the next card's id; `app.js` fetches `/inbox/card/<next-id>` after the current card settles and keeps the HTML in memory, swapping it in when the user presses J.
