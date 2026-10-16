from fastapi import APIRouter

from codemind.api.v1.routes import files, jobs, repositories

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(repositories.router)
api_router.include_router(jobs.router)
api_router.include_router(files.router)
