from fastapi import APIRouter

router = APIRouter(tags=["Health"])


@router.get("/health")
def health() -> dict[str, str]:
    """Check whether the API process is responding."""
    return {"status": "healthy"}
