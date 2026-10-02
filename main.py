"""FastAPI host application with the plugin runtime wired in."""

from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI

from plugin_runtime.api import router, set_runtime
from plugin_runtime.runtime import PluginRuntime


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: create the runtime.
    runtime = PluginRuntime(
        drain_timeout=30.0,
        failure_threshold=5,
    )
    set_runtime(runtime)
    app.state.runtime = runtime
    yield
    # Shutdown: nothing to clean up (plugins are in-process).


app = FastAPI(title="Plugin Runtime Host", lifespan=lifespan)
app.include_router(router)


@app.get("/")
def read_root():
    return {
        "service": "plugin-runtime-host",
        "docs": "/docs",
        "api": "/api/v1",
    }


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=0)
