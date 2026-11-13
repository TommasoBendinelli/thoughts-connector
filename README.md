# Thoughts connector

An MCP server that lets ChatGPT and Claude (web, desktop and phone apps) work with a
Thoughts Library (the Thoughts server and its desktop and Android apps): read and search documents,
see review cards and the passages the user liked or disliked, send notes to the phone, and suggest
edits as agent proposals that the user accepts or rejects in the Thoughts apps.

It runs next to a Thoughts server, behind an HTTPS reverse proxy, and is its own OAuth 2.1
authorization server: the user connects it once in ChatGPT or Claude and signs in with a passphrase.

By **Tommaso Bendinelli**.

## What it can do

Tools: `list_documents`, `read_document` (LaTeX with `\input` files inlined, Markdown, or PDF text),
`search_library`, `document_feedback`, `review_cards` / `get_review_card`, `knowledge_profile`
(which cards on a topic are solid, shaky, unknown or unreviewed), `link_conversation`, `list_notes`,
`get_note` (with the user's replies, their drawings and the photo sent with a reply), `send_note`
(Markdown with math rendered to a phone-width PNG), `add_diary_entry`, `read_source`,
`create_proposals` / `revise_proposal` / `remove_proposal`, `source_conflicts` /
`read_source_conflict` / `resolve_source_conflict`, `create_document`, `create_review_card` /
`move_review_card`, `merge_review_cards` (cards of one document merged into one, when the user asks),
`pending_card_moves` / `cancel_pending_card_move` (a card moved onto the text of a pending proposal
waits until the user accepts that proposal), `html_pages` / `add_html_page`, `build_status`,
`move_pdf`, `import_pdf` (the server downloads a PDF from a URL into a Project).

Reply listening: while `get_note` waits for a reply (`wait_for_reply_s`), or while
`thoughts-watch-replies` runs, the server knows that an agent listens, and the phone shows the user
whether their reply will be heard; `send_note` says when nobody listens.

The connector talks to the Thoughts server as its own device (`connector`), and every request goes
through an allowlist in `thoughts_connector.py` (`check_request`): it reads the Library, creates
agent notes, diary entries and conversation links, and publishes sources into which thoughts-cli
wrote *pending* proposals, and asks the server for a PDF import by URL, a merge of review cards or a
pending card move. It never accepts or rejects a proposal and never reviews or retires a card; cards
are deleted only by the server, as part of a merge the user asked for. It refuses every other
Library-project action.

## Requirements

- Python 3.11+ with the packages in `requirements.txt` (`mcp`, `pypdf`, `httpx`; `uvicorn` comes with `mcp`).
- A Thoughts server reachable from the connector (default `http://127.0.0.1:8080`) and a device token
  for the connector: `thoughts-admin create-device connector` on the server host.
- `thoughts_cli` (the headless proposal client, `clients/thoughts-cli/thoughts_cli` in the
  thoughts-server repository) next to `thoughts_connector.py`, or `THOUGHTS_CLI` pointing at it.
- For `send_note`: `agent_notes.py` from the Thoughts desktop repository (`tools/mcp/agent_notes.py`)
  next to `thoughts_connector.py` or at `THOUGHTS_AGENT_NOTES`, and Chromium or Google Chrome
  (`THOUGHTS_CHROME`).
- `import_pdf`, `merge_review_cards` and pending card moves need a Thoughts server that announces
  the capabilities `library_import_pdf_v1`, `review_card_merge_v1` and
  `review_card_pending_moves_v1`, and a thoughts-cli with `merge-review-cards` and pending card
  moves; without them the tools answer `not_supported`.

## Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `CONNECTOR_PUBLIC_URL` | `https://thoughts.example.org` | public origin of the reverse proxy (the connector lives under `/connector`) |
| `CONNECTOR_PASSPHRASE` | `~/connectors/passphrase.json` | scrypt hash of the sign-in passphrase |
| `THOUGHTS_URL` | `http://127.0.0.1:8080` | the Thoughts server |
| `THOUGHTS_CONNECTOR_TOKEN` | `~/connectors/thoughts-token` | file with the connector's device token |
| `THOUGHTS_CONNECTOR_STATE` | `~/connectors/state/thoughts` | OAuth clients, hashed tokens, request records |
| `THOUGHTS_CONNECTOR_PORT` | `8095` | loopback port |
| `THOUGHTS_CLI` | the connector's folder | where `thoughts_cli` is |
| `THOUGHTS_AGENT_NOTES` | `agent_notes.py` next to the connector | the note renderer |
| `THOUGHTS_CHROME` | see `agent_notes.py` | Chromium/Chrome for rendering notes |
| `THOUGHTS_DIARY_AUTHOR` | `user` | author name written on diary entries |
| `THOUGHTS_DIARY_ZONE` | `Europe/Zurich` | time zone of diary entries and of "this week" in `knowledge_profile` |

## Running it

```sh
python3 -m venv venv && venv/bin/pip install -r requirements.txt
venv/bin/python thoughts_connector.py set-passphrase   # prints a new passphrase once
venv/bin/python thoughts_connector.py serve            # or install thoughts-connector.service (systemd --user)
```

Other commands: `status` (connected clients), `revoke-all` (disconnect every client and browser).

The reverse proxy must forward `/connector/*` and the OAuth discovery documents
`/.well-known/oauth-protected-resource/connector/mcp` and
`/.well-known/oauth-authorization-server/connector` (and the `openid-configuration` spellings) to
`127.0.0.1:8095`. With Caddy:

```caddy
thoughts.example.org {
    @connector path /connector /connector/* /.well-known/oauth-protected-resource/connector/* /.well-known/oauth-authorization-server/connector /.well-known/openid-configuration/connector
    reverse_proxy @connector 127.0.0.1:8095
}
```

`thoughts-watch-replies` is an optional monitor for agents that run on the server host: it prints
one line per reply the phone sends to a note of the connector, and tells the server that an agent
listens. It uses the connector's token and `agent_notes.py` (`THOUGHTS_AGENT_NOTES`);
`thoughts-watch-replies --check` tests the connection.

Then add `https://<your host>/connector/mcp` as a custom connector in ChatGPT or Claude.
`chatgpt-plugin/thoughts/` is a plugin manifest for the same URL; replace the host in `mcp.json`.

Only ChatGPT and Claude redirect URIs (or a loopback port for their desktop apps) may register as
OAuth clients; five wrong passphrases from one address lock the sign-in for 15 minutes.

## Tests

```sh
pip install pytest
THOUGHTS_CLI=/path/to/thoughts-server/clients/thoughts-cli pytest tests
```

Set `THOUGHTS_AGENT_NOTES=/path/to/thoughts-desktop/tools/mcp/agent_notes.py` to also run the test
of `thoughts-watch-replies` (skipped without it).

The tests run the connector against a fake Thoughts server and check every tool end to end through
OAuth and MCP. Without `THOUGHTS_CLI`, they look for a `thoughts-server` checkout next to this one;
the proposal tools' tests are skipped when thoughts-cli is missing or too old.

## Licence

GPL-2.0-or-later, like the Thoughts server and desktop app; see `LICENSE` and
`LICENSES/GPL-2.0-or-later.txt`. Third-party notices: `THIRD-PARTY-NOTICES.md`.
