# Third-party notices

The Thoughts connector bundles no third-party code. It is used together with:

| Component | Licence | How |
| --- | --- | --- |
| [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk) (`mcp`, with Starlette, Uvicorn, httpx, Pydantic) | MIT / BSD-3-Clause | installed from PyPI (`requirements.txt`) |
| [pypdf](https://github.com/py-pdf/pypdf) | BSD-3-Clause | installed from PyPI |
| thoughts-cli (`clients/thoughts-cli` of the Thoughts server) | GPL-2.0-or-later | copied next to the connector or found via `THOUGHTS_CLI` |
| `agent_notes.py` (`tools/mcp` of the Thoughts desktop app) and the KaTeX it loads | GPL-2.0-or-later / MIT | copied next to the connector or found via `THOUGHTS_AGENT_NOTES`; loaded by `send_note` and by `thoughts-watch-replies` |
| Chromium / Google Chrome | various | called as a program to render notes |
