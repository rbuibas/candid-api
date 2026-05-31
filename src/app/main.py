from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import get_settings
from app.routers import dev, devices, groups, health, posts, profile, prompts


def create_app() -> FastAPI:
    settings = get_settings()
    api = FastAPI(title="candid-api")

    origins = [o.strip() for o in settings.cors_allow_origins.split(",") if o.strip()]
    api.add_middleware(
        CORSMiddleware,
        allow_origins=origins or ["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    api.include_router(health.router)
    api.include_router(profile.router)
    api.include_router(groups.router)
    api.include_router(posts.router)
    api.include_router(devices.router)
    api.include_router(prompts.router)
    api.include_router(dev.router)
    return api


app = create_app()
