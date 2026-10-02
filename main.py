import uvicorn
from fastapi import FastAPI

from plugin_runtime.api import create_app
from plugin_runtime.runtime import PluginRuntime

runtime = PluginRuntime()
app = FastAPI()
app.mount("/plugins-api", create_app(runtime))


@app.get("/")
def read_root():
    return {"Hello": "World"}


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=0)
