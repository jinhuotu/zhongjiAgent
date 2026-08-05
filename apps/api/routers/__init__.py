from fastapi import APIRouter

from api.routers import ai, auth, health, knowledge

api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(auth.router)
api_router.include_router(knowledge.router)
api_router.include_router(ai.router)
