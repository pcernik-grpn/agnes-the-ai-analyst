import os

import uvicorn

from services.kai_engine_stub.api import app

if __name__ == "__main__":
    # 3000 is the port the default `chat.kai_agent_url` names, so the stub is
    # reachable with no extra configuration in compose and with only
    # AGNES_CHAT_KAI_AGENT_URL=http://127.0.0.1:3000 on a laptop.
    uvicorn.run(app, host=os.environ.get("KAI_STUB_HOST", "0.0.0.0"), port=int(os.environ.get("KAI_STUB_PORT", "3000")))
