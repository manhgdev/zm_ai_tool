"""Web from source; the packaged APP launcher sets ZM_AI_TOOL_DESKTOP=1."""
from __future__ import annotations

import os
from pathlib import Path

if os.environ.get("ZM_AI_TOOL_DESKTOP") == "1":
    from api.app import create_app
else:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=False, interpolate=False)
    from web import create_web_app as create_app

app = create_app()

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=os.environ.get("ZM_AI_TOOL_HOST", "127.0.0.1"),
                port=int(os.environ.get("ZM_AI_TOOL_PORT", "8787")), access_log=False,
                forwarded_allow_ips=os.environ.get("ZM_AI_TOOL_WEB_PROXY_IPS", "127.0.0.1"))
