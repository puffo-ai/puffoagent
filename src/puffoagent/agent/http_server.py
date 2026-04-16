import json
import logging
from aiohttp import web

logger = logging.getLogger(__name__)

CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type",
}


def json_response(data) -> web.Response:
    return web.Response(
        text=json.dumps(data),
        content_type="application/json",
        headers=CORS_HEADERS,
    )


def build_app(agent) -> web.Application:
    app = web.Application()

    async def handle_options(request: web.Request) -> web.Response:
        return web.Response(headers=CORS_HEADERS)

    async def get_profile(request: web.Request) -> web.Response:
        return json_response(agent.profile_files())

    async def get_usage(request: web.Request) -> web.Response:
        return json_response(agent.usage.stats())

    app.router.add_route("OPTIONS", "/api/profile", handle_options)
    app.router.add_route("OPTIONS", "/api/usage",   handle_options)
    app.router.add_get("/api/profile", get_profile)
    app.router.add_get("/api/usage",   get_usage)

    return app


async def start_http_server(agent, port: int = 8066):
    app = build_app(agent)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info(f"Agent HTTP API listening on http://0.0.0.0:{port}")
