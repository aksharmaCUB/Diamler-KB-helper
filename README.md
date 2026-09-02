# KB Helper

A chat assistant for "how do I ..." questions at work. Ask *"How do I deploy the pricing service to
staging?"* or *"How do I raise an IT ticket?"* and it searches your company knowledge sources
(SharePoint today, more later), reads the relevant documents, and answers with steps and links.
When the request is ambiguous or a procedure needs details you have not given (environment,
ticket type, team...), it asks you first instead of guessing.

```
you> how do I deploy?
  [searching: deploy]
  [reading sharepoint:driveItem:...]

Which environment do you want to deploy to?
Options: [1] staging | [2] production
_The steps differ: production needs an approved change ticket._

you> 2
...
```

## How it works

```
Browser / CLI ──► FastAPI (kb_helper/server.py) ──► Assistant (kb_helper/agent.py, Claude)
                                                       │  tools: search_knowledge_base,
                                                       │         read_document, ask_user
                                                       ▼
                                          Connectors (kb_helper/connectors/)
                                          ├── sharepoint   (Microsoft Graph)
                                          ├── local_folder (files on disk)
                                          └── your own ... (Confluence, Jira, wiki, ...)
```

* **Connectors** turn a backend into two operations, `search(query)` and `fetch(document_id)`.
  They are configured in `config.yaml`; any number can be active at once.
* **The assistant** is a Claude agent loop (`claude-opus-5` by default). Claude decides when to
  search, which documents to read, and when to call `ask_user`. A question ends the turn; your
  reply continues the same conversation.
* **Text extraction** handles Word, PDF, Excel, PowerPoint, HTML/modern SharePoint pages,
  SharePoint list items, Markdown and plain text.

## Quick start

```bash
git clone <this repo> && cd Diamler-KB-helper
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env            # put your ANTHROPIC_API_KEY here
cp config.example.yaml config.yaml

# Try it with the bundled sample documents first (no SharePoint needed):
python -m kb_helper.cli -q "how do I create a ticket for VPN access?"

# Web UI:
python -m kb_helper.server      # then open http://127.0.0.1:8000
```

The example config enables both `sharepoint` and `local-docs`. Remove or disable the ones you
do not want.

## Connecting SharePoint

### Option A (default): sign in with your own account - no app registration

Set `auth_mode: user` (the default). The first time you ask something, the helper shows a
short code and a Microsoft link:

* **Web UI** - a *Sign in to sharepoint* button appears in the header (and pops up automatically
  when needed). Click it, open the link, enter the code, sign in with your normal work account
  (MFA works). The chat notices when you are done.
* **CLI** - the code is printed in the terminal.

The helper then acts as *you*: it can only read what your account can read. Refresh tokens are
cached under `token_cache_dir` (default `.kb_helper_tokens/`, one file per web session or
`local.json` for the CLI), so you sign in once, not every run. Use *Sign out* to drop them.

This uses Microsoft's built-in public client *Microsoft Graph Command Line Tools*
(`14d82eec-204b-4c2f-b7e8-296a70dab67e`) and requests the delegated permissions
`Sites.Read.All`, `Files.Read.All`, `User.Read`. Two things can get in the way:

1. **Your tenant blocks user consent.** You will see an "Need admin approval" page on first
   sign-in. An admin can approve the *Microsoft Graph Command Line Tools* app once for the
   organisation (Entra admin center > Enterprise applications), or register a public client of
   your own and put its id in `client_id`.
2. **Conditional access** rules that block device-code sign-in. In that case ask IT for a
   public client app registration with "Allow public client flows" enabled, or use option B.

Optional settings: `tenant_id` (your tenant domain, if `organizations` does not work) and
`sites` (list of site URLs to search; strongly recommended so results are not polluted by
unrelated sites).

### Option B: app registration with client secret (service identity)

For a shared bot that should work without anyone signing in. In the Entra admin center create an
app registration, add **application** permissions `Sites.Read.All` and `Files.Read.All` (grant
admin consent), create a client secret, and configure:

```yaml
options:
  auth_mode: client_credentials
  tenant_id: ${AZURE_TENANT_ID}
  client_id: ${AZURE_CLIENT_ID}
  client_secret: ${AZURE_CLIENT_SECRET}
  sites: [https://contoso.sharepoint.com/sites/IT]
  search_region: EMEA   # required by the Microsoft Search API for app-only calls
```

Everyone who can talk to the bot can then read everything the app can read, so scope
permissions (or use `Sites.Selected`) accordingly.

### What is searched

The connector uses the Microsoft Search API (the same engine as the SharePoint search box) over
files and list items, restricted to the configured `sites`. If the Search API is not available it
falls back to searching each document library of those sites. Modern site pages are read through
the Graph Pages API so the text of web parts is included.

## Configuration reference

```yaml
assistant:
  model: claude-opus-5     # Claude model id
  effort: high             # low | medium | high | xhigh | max
  max_tokens: 16000
  fallbacks: true          # server-side refusal fallback (Claude API only)
  max_tool_rounds: 12      # max search/read steps per question
  extra_instructions: |    # company-specific guidance appended to the system prompt
    ...
server: { host: 127.0.0.1, port: 8000 }
connectors:
  - name: <unique name>
    type: sharepoint | local_folder | package.module:ClassName
    description: <shown to the model so it knows what the source contains>
    enabled: true
    options: { ... connector-specific ... }
```

`${VAR}` and `${VAR:-default}` inside the YAML are replaced from the environment (a `.env`
file is loaded automatically). `KB_HELPER_CONFIG` points to a different config file;
`KB_HELPER_MODEL` overrides the model.

## HTTP API

| Method | Path | Purpose |
|--------|------|---------|
| POST | `/api/chat` | `{"message": "...", "session_id": "..."}` -> `{kind: answer|question|error, text, options, sources, events, auth_required, session_id}` |
| GET | `/api/sessions/{id}` | transcript of a session |
| POST | `/api/sessions/{id}/reset` | forget a conversation |
| GET | `/api/connectors` | configured sources |
| GET | `/api/health` | model, connector status, config errors |
| GET | `/api/auth?session_id=` | which connectors need a personal sign-in and whether this session has one |
| POST | `/api/auth/{connector}/start?session_id=` | start a device-code sign-in; returns `user_code` + `verification_uri` |
| GET | `/api/auth/{connector}/status?session_id=` | `pending` / `signed_in` / `signed_out` / `error` |
| POST | `/api/auth/{connector}/signout?session_id=` | drop the cached tokens |

Conversations are kept in memory (per `session_id`, 6 h idle timeout). The bundled UI stores its
session id in the browser's local storage. Anyone who can reach the port can chat and, with
user sign-in, use *their own* SharePoint access; put the server behind your usual reverse proxy /
SSO if you expose it beyond localhost.

## Adding another knowledge source

1. Subclass `kb_helper.connectors.Connector`, set `type_name`, implement `search` and `fetch`
   (see `local_folder.py` for a compact example, `sharepoint.py` for a full one). Raise
   `ConnectorError` for problems the assistant should report to the user.
2. If users must sign in themselves, return an object with `start_login`, `status`, `sign_out`
   from `login_provider()` (the SharePoint connector's `UserLoginAuth` is reusable for any
   Microsoft Graph-based source such as OneDrive or Teams).
3. Reference it in `config.yaml` by dotted path (`type: my_pkg.mod:MyConnector`) or register
   it with `register_connector_type` and use the short name.

The model sees every enabled connector's name and description and can target one or search all.

## Development

```bash
pip install -r requirements-dev.txt
pytest
```

Tests cover text extraction, both connectors (SharePoint against a mocked Graph API), the
registry, the sign-in flow (mocked MSAL), the agent loop (scripted Claude responses) and the
HTTP API. No network or credentials are needed to run them.
