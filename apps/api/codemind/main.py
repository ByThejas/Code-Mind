from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from codemind.api.v1.health import router as health_router
from codemind.api.v1.router import api_router
from codemind.core import handlers
from codemind.core.config import settings

app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    description=("An AI software engineering workspace that understands entire code repositories."),
    debug=settings.debug,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/", tags=["General"])
def root() -> dict[str, str]:
    """Return basic application information."""
    return {
        "name": settings.app_name,
        "version": settings.app_version,
        "status": "running",
    }


app.include_router(health_router)
app.include_router(api_router)
handlers.install(app)
