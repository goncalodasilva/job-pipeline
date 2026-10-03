"""App factory and lifespan (spec 2.6, 2.13; ADR-030).

The lifespan opens the database, applies pending migrations, starts the task runner and
builds the services; shutdown stops the runner before closing the database. Logging is set
up by the CLI before the app is created.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI

from app import __version__
from app.api.health import router as health_router
from app.api.pages import register_pages
from app.api.security import SecurityMiddleware, generate_launch_token, page_route_matcher
from app.config import RuntimeConfig
from app.domain.clock import Clock, SystemClock
from app.modules.storage import MIGRATIONS_DIR, Database, apply_migrations
from app.services.health import HealthService
from app.tasks import TaskRunner

DATABASE_FILE = "app.db"
BACKUP_FOLDER = "backups"


def create_app(
    config: RuntimeConfig,
    *,
    clock: Clock | None = None,
    migrations_dir: Path = MIGRATIONS_DIR,
) -> FastAPI:
    app_clock = clock if clock is not None else SystemClock()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        db = Database.open(config.data_dir / DATABASE_FILE)
        runner = TaskRunner()
        try:
            apply_migrations(db, migrations_dir, config.data_dir / BACKUP_FOLDER, app_clock)
            await runner.start()
            app.state.db = db
            app.state.runner = runner
            app.state.health = HealthService(db, lambda: runner.status, __version__)
            yield
        finally:
            try:
                await runner.stop()
            finally:
                db.close()

    # No generated API docs: page GETs need no token, so they would expose every route.
    app = FastAPI(
        title="Job Search Pipeline",
        version=__version__,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    launch_token = generate_launch_token()
    app.state.launch_token = launch_token
    # Added last, so it stays the outermost user middleware: nothing runs before these checks.
    app.add_middleware(
        SecurityMiddleware,
        port=config.port,
        launch_token=launch_token,
        is_page=page_route_matcher(lambda: app.router.routes),
    )
    app.include_router(health_router)
    register_pages(app)
    return app
