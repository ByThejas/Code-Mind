from fastapi import APIRouter

from codemind.api.v1.routes import files, graph, investigations, jobs, repositories, search

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(repositories.router)
api_router.include_router(jobs.router)
api_router.include_router(files.router)
api_router.include_router(search.router)
api_router.include_router(investigations.router)
api_router.include_router(graph.router)
