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
<ul class="board-cards" data-status="{{ status.key }}" data-sortable>
  {% for application in column %}
  <li class="board-card" data-application-id="{{ application.id }}"
      data-version="{{ application.version }}" data-status="{{ application.status_key }}">
    {# Follow the real markup in board/_card.html and applications/_detail.html. #}
    <button type="button" aria-haspopup="menu" aria-expanded="false">Move to…</button>
    <form method="post" action="/applications/{{ application.id }}/status"
          hx-post="/applications/{{ application.id }}/status" hx-target="#board-content" hx-swap="outerHTML">
      <input type="hidden" name="version" value="{{ application.version }}">
      <input type="hidden" name="source" value="menu">
      <div role="menu" hidden>
        {% for s in statuses %}<button type="submit" role="menuitem" name="status" value="{{ s.key }}">{{ s.label }}</button>{% endfor %}
      </div>
    </form>
  </li>
  {% endfor %}
</ul>
```

`board.js` initializes SortableJS on `[data-sortable]`; a drop sends the same versioned status POST with `source=board`. `status-menu.js` provides keyboard/pointer menu behavior; drawer forms post `source=menu`, and the server records `drawer` from the application-detail target. Keep the real menus' classes, unique control ids and labels when reusing them. The illustrative markup above is abbreviated. A refused drop keeps the old position and announces the reason. Sortable is destroyed before replacement and recreated after swaps, disabled at phone width, and has no animation with reduced motion.

## Calendar download

```html
<button type="button" data-calendar-download="/applications/{{ application.id }}/events/{{ event.id }}/ics">Download calendar file</button>
```

The delegated helper in `app.js` validates the safe same-origin path, sends `X-JP-Token` from the body's `hx-headers` in a fetch header, checks the response and its calendar content type, and saves a blob with a temporary object URL as `jobpipeline-event.ics`. It disables/restores the button and announces the result. The token is never a query parameter. This is a protected, read-only in-memory export, not a normal navigation or a background task; do not add file writes, secret reads or external service/network calls to its GET. Calling the local in-memory calendar provider is the approved formatter boundary.

## Prefetching the next inbox card

The queue fragment includes the next card's id; `app/static/js/shortcuts.js` fetches `GET /inbox/jobs/<next-id>/card` after the current card settles and keeps the HTML in memory for 30 seconds, swapping it in when the user presses J. It prefetches only same-origin `/inbox/jobs/` paths, and the GET marks nothing seen. The shown card sends its own `POST /inbox/jobs/<id>/seen` (`hx-trigger="load"`); a key pressed before that answer is held until it arrives, because the answer changes the job's version.

## Filter form that pushes the page address

```html
<form action="/inbox" method="get" hx-get="/inbox/queue" hx-target="#inbox" hx-swap="outerHTML settle:0ms">
  <input type="hidden" name="push" value="1">
  <label for="filter-band">Fit</label>
  <select id="filter-band" name="band">...</select>
  <button type="submit">Apply filters</button>
</form>
<a href="/inbox?view=queue" hx-get="/inbox/queue?view=queue&amp;focus=1" hx-target="#inbox"
   hx-push-url="/inbox?view=queue">Clear filters</a>
```

Without JavaScript the form loads `/inbox?...` as a page. With htmx the fragment route answers with `HX-Push-Url: /inbox?band=strong` (validated values, built on the server) when `push=1` is sent, never `/inbox/queue?...`: a reload of the fragment route answers 403. A link that changes the view carries `hx-push-url` with the page address.

## Page with an id, no token

```python
app.add_api_route("/jobs/{job_id}", job_page, methods=["GET", "HEAD"], tags=[PAGE_TAG],
                  response_class=HTMLResponse, name="page_job")

def job_page(request: Request, job_id: str) -> Response:
    if not is_safe_id(job_id):
        return PlainTextResponse(JobNotFound.message, status_code=404)  # echoes nothing
    ...  # read only; a GET never changes the job
```

The drawer, the header and every write for the same job are separate routes under `/inbox/jobs/{id}/...` that need the token.
