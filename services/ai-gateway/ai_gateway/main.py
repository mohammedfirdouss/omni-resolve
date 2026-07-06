"""Production entrypoint for the AI Gateway (LiteLLM-backed)."""

from __future__ import annotations

import os

from ai_gateway.app import create_app

app = create_app()

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8100")))
