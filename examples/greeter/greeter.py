class GreeterPlugin:
    async def handle(self, capability, payload, ctx):
        if capability == "greet":
            name = payload.get("name", "world")
            return {"message": f"Hello, {name}!"}
        raise ValueError(f"unknown capability {capability}")

def create_plugin():
    return GreeterPlugin()
