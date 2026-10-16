from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from codemind.core.config import settings

app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    description=(
        "An AI software engineering workspace that understands "
        "entire code repositories."
    ),
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


@app.get("/health", tags=["Health"])
def health() -> dict[str, str]:
    """Check whether the API process is responding."""
    return {"status": "healthy"}