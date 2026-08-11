# Agent Team Workspace

A local web workspace whose original workspace is seeded with six specialist agents—Orchestrator, Researcher, Programmer, Reviewer, Formatter, and Documenter—with extensible roles, provider-specific conversations, and MCP-backed shared context.

## What it does

- Talk to each team member in an independent, persistent conversation.
- Let agents send durable **command** or **report** messages directly into any other agent's project-scoped chat. Messages retain their sender and relationship type, appear in the recipient's groupchat transcript, and automatically start an idle recipient run; messages sent while a run is active remain queued and are synthesized into its next prompt.
- Multiple unsent commands from the same sender to the same recipient are coalesced into one compiled command request. The transcript renders that bundle as a numbered command card, while every provider receives it as ordinary `[COMMAND from …]` text (never the storage envelope). Once a command has entered a provider prompt, later commands remain separate and are delivered on the following prompt.
- Assign shared context to everyone or selected roles from the UI.
- Let agents read and publish shared context through a local MCP server.
- Let the Orchestrator hand a request to the most appropriate specialist.
- Choose a provider and model independently for every role: OpenAI, Codex, Google Gemini, or an OpenAI-compatible server.
- Load each provider's available models into a dropdown instead of entering model IDs manually.
- Retrieve the signed-in Codex account's complete model catalog and each model's supported reasoning efforts.
- Set a persistent default model/effort per agent or override both for an individual human chat message.
- Add custom team members manually or generate a complete role draft with your Codex subscription.
- Create portable ACP-compatible Agent Skills manually or generate them with Codex. Each package contains a standards-compatible `SKILL.md` (YAML frontmatter plus Markdown instructions), optional `scripts/`, `references/`, and `assets/`, versioned OS variants, and workspace-specific agent assignments. Assigned agents discover skills progressively through MCP (`list_assigned_skills` → `load_assigned_skill` → `read_skill_resource`/`run_assigned_skill`); humans can manage them from **Skills** in the top bar or `/app skill <skill_id> <JSON>` in chat.
- Create project-local command-line toolsets manually or generate a complete reviewable draft with Codex from **Tools** in the top bar. Toolsets are written to `.agents/tools/<toolset>/`, assigned per agent, and advertised through a short prompt catalog. An agent loads only a relevant `TOOLSET.md` summary and requests execution with `TOOLCALL - <toolset>/<tool> - [arguments].`; the app runs the configured file and replaces that marker with the tool's formatted result before saving the chat response. Generic repository commands use `COMMAND - <text of command>` so the app runs them on the selected local project rather than in a provider's development environment; unapproved commands create an in-chat permission request.
- Optionally enable a Git workflow while adding an agent, or later from an existing agent's right-click menu. The app asks for the main branch (and offers explicit repository initialization when needed), can add or update a named remote from its URL, gives every Git-enabled agent its own role-named branch, and merges each completed agent commit into main. The **Git** button exposes per-file diff summaries; a commit can be viewed in PyCharm or VS Code, safely reverted with a follow-up commit, rolled back only when it is the current HEAD merge, or pushed to GitHub.
- Search the SkillsMP marketplace from the Skills dialog, filter installed packages by type, sort them by name/type/source/recent update, and import a GitHub-backed `SKILL.md` package into the local library. Marketplace scripts are downloaded for review and are not assigned automatically. Set `SKILLSMP_API_KEY` for the marketplace's higher authenticated quota, or leave it unset for anonymous search.
- Marketplace installs stay in the dialog and stream download, extraction, and installation progress to the progress bar. The stream endpoint uses the upstream archive's byte count when available and falls back to an indeterminate progress state.
- Store each workspace's agent definitions, runtime settings, context, conversations, Codex sessions, and graph locally in SQLite. New workspaces start with a blank graph; agents must be added to that workspace explicitly.
- Use messenger-style chat bubbles with timestamps, persisted reply threads, copy actions, and a queue-aware composer. You can submit another human command while an agent is working; it is persisted as its own transcript bubble and runs on the next turn, where it can be synthesized with any pending team commands or reports.
- Render fenced Markdown code with a PyCharm-inspired palette. Every block can be copied; Python, JavaScript/Node, POSIX shell, and Ruby blocks can be run after confirmation in the selected project's local terminal pane, with stdout, stderr, exit status, timeout, and runtime errors shown.
- Preserve failed provider calls in the chat with their HTTP status, error code/type, request ID, message, and provider response body for troubleshooting.
- Attach up to six 10 MB images or supported documents per message. Files remain local under the ignored `data/uploads/` directory and are sent using each provider's supported input format.
- Create and switch between Codex-style project workspaces with optional local folder references.
- Arrange agents on a persistent flowchart and change their reporting relationships visually.
- Type `/` in any agent chat for command autocomplete. The menu combines the active provider's native catalog (Codex commands are included), app-only commands, and provider commands you have previously used. App commands are namespaced as `/app …` so provider-native slash commands are never intercepted; `/gh` is the one explicit repository-report command.
- Every Codex-backed project/agent chat owns a persistent headless Codex CLI session. Its thread ID is stored in SQLite and resumed after app shutdown or restart; each turn also receives the latest shared-context snapshot. Clearing that chat's history starts a new Codex session.
- Codex slash commands are sent verbatim to the same saved session through the real interactive terminal when one exists. `/status`, for example, returns the exact Codex status panel—including its model, permissions, account, session, and usage limits—rather than a response reconstructed by this app. Other providers receive slash commands as normal model prompts because their API protocols do not define a shared native slash-command catalog; those commands remain available for passthrough and are learned by autocomplete after first use.
- `/gh` reports the selected project's local repository path, branch, HEAD, working-tree status, staged and unstaged changes, recent commits, and remotes without changing files.

Agents with MCP/tool support can call `send_agent_message` and `list_agent_messages` directly. The REST equivalents are `POST /api/agents/{sender_role}/messages` and `GET /api/agents/{recipient_role}/inbox`; both require a `project_id`, and a message relationship must be `command` or `report`.

The app opens on the project/team map. Drag a team-member card to reposition it, use its **Reports to** menu to change the hierarchy, and double-click it to open that agent's chat. Creating or switching projects alone does not modify the selected folder; explicit actions such as saving a local toolset, materializing an assigned skill, or running code can write or execute within it.

In chat, **Enter** sends and **Shift+Enter** inserts a newline. The transcript has its own scroll region so the app shell and composer stay in place. Use arrow keys and Tab to navigate slash-command completion; pressing Enter on a highlighted command selects and runs it.

## Run locally

Python 3.11–3.13 is recommended.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
python main.py
```

Open the URL printed at startup. The app starts at port `8000` and automatically advances to the next free port (`8001`, `8002`, and so on) if needed. Set `PORT_START` only if you want a different starting point. The app loads `OPENAI_API_KEY` from the ignored `.env.local` file. Optionally set `OPENAI_MODEL`; the default is `gpt-5-mini`.

### Provider setup

Open **Runtime** beside any agent to choose its provider and model. Runtime profiles contain no secret values.

- **Codex:** install/sign in to Codex CLI. The app checks its inherited `PATH`, standard macOS install locations, JetBrains' bundled runtime, and your login shell. Set `CODEX_COMMAND=/absolute/path/to/codex` only when automatic discovery cannot find it, then restart the app.
- **OpenAI:** set `OPENAI_API_KEY` in `.env.local`.
- **Google Gemini:** set `GEMINI_API_KEY` in `.env.local`; requests use Google's OpenAI-compatible endpoint.
- **Anthropic Claude:** connect an `ANTHROPIC_API_KEY`; requests use Anthropic's OpenAI SDK compatibility endpoint.
- **OpenAI-compatible:** enter the endpoint URL in the Runtime dialog, such as `http://127.0.0.1:11434/v1` for a same-laptop server or `http://192.168.1.20:11434/v1` for another machine. If authentication is required, set the named environment variable (default `LOCAL_API_KEY`) in `.env.local`.

The remote model server must listen on a reachable interface and its firewall must allow the app laptop. Models without function/tool calling can still be conversational, but cannot reliably use MCP tools.

The Gemini bridge preserves the provider's `extra_content.google.thought_signature` when it handles an inter-agent function call, and retries text-only if the selected model rejects tools.

Open **Settings → Accounts** in the app header to connect Codex with ChatGPT device authorization or securely save/remove OpenAI, Gemini, Anthropic, and OpenAI-compatible endpoint credentials. Saved keys are written only to the ignored `.env.local` with owner-only file permissions and are never returned to the frontend.

Executable skill helpers receive one JSON object on stdin and in `SKILL_INPUT_JSON`; they should print one JSON value to stdout. The runner selects the current OS variant (`macos`, `linux`, `windows`, then `any`), enforces a 30-second timeout and output cap, and returns both structured output and terminal diagnostics. Instruction-only skills are valid and are loaded from `SKILL.md` rather than executed. Assignments are checked before either a human or an agent can run a skill. ACP packages assigned to a project are materialized under `.agents/skills/<skill-name>` so compatible Codex/agent clients can discover them natively.

Skills may declare `required_secrets` as a JSON array of environment-variable references, for example `[{"name":"WEATHER_API_KEY","label":"Weather API key","required":true}]`. Add or edit those declarations in the Skills editor, then save each value in the same editor. Skill values are stored only in the ignored, owner-readable `data/.skill-secrets.local` file, while provider account keys remain in `.env.local`; the browser sees only configured/missing status. The runner injects only declared values into that skill's child process and redacts them from returned stdout/stderr. Marketplace packages are never allowed to provision or request a value automatically; review a package before assigning it. For production deployments, replace the local credential store with an OS keychain or secret manager.

Tool arguments use a JSON list and are passed directly as positional process arguments rather than interpolated into a shell command. Python, JavaScript, PowerShell, POSIX shell, and native executable files are supported when their runtime is installed. Tool processes run in the selected project directory with a 60-second timeout and output cap. They receive a small safe environment plus only the additional environment-variable names explicitly declared while creating that tool; declared values are redacted from returned stdout and stderr. Result templates may use `{stdout}`, `{stderr}`, `{exit_code}`, `{toolset}`, and `{tool}`, and can render as text, Markdown, JSON, or a code block.

Git-enabled agents all work from their own branch, named after the agent role, and the app automatically commits the workspace then merges that branch into the configured main branch after each provider run. Set local `git user.name` and `git user.email` before the first run. The setup dialog can add or update a remote named `gh` from a GitHub repository URL; the remote name is separate from branch names, and pushes use matching local-to-remote branch names. To open file comparisons externally, install the `pycharm` or `code` command-line launcher, or set `PYCHARM_COMMAND` or `VSCODE_COMMAND` to its executable path. **Revert** preserves history with a new inverse commit, while **Rollback HEAD** performs a hard reset and is intentionally limited to the most recent agent-tracked merge.

The team map is a many-to-many directed graph. Drag the blue square from a commanding agent onto an agent it may command; command edges are solid and guide durable `send_agent_message` delegation. Drag the green dot from a subordinate onto an agent it reports to; reporting edges are dotted. Every agent can have multiple incoming and outgoing edges of both kinds. Use × on a relationship chip to remove that edge.

Right-click any agent card on the team map to change its provider/model, save its role description as a reusable template, or remove it. Removing an agent requires confirmation and cleans up its runtime, conversation history, positions, and graph edges. Saved templates can be selected in the **Add team member** dialog.

Run tests with:

```bash
pytest
```

## Architecture

- `main.py`: FastAPI API, UI hosting, and application lifecycle.
- `team.py`: Agents SDK role definitions, durable delegation, and SQLite chat sessions.
- `skills.py`: ACP/Agent-Skills package index, SKILL.md parser/writer, OS/version selection, marketplace client, project materialization, resource loading, assignments, and script runners.
- `toolsets.py`: project-local toolset manifests, summary documents, assignments, `TOOLCALL`/`COMMAND` parsing, local process launch, and chat-result formatting.
- `git_workflow.py`: shared-branch Git configuration, run commits, file summaries/diffs, editor launch, revert/rollback, and remote push operations.
- `mcp_server.py`: stdio MCP server exposing shared-context, reusable-skill, and inter-agent messaging tools.
- `shared_context.py`: deterministic SQLite context store used by the MCP server and UI.
- `static/`: dependency-free browser interface.
- `data/`: ignored local SQLite state.

The skill package contract is the open Agent Skills `SKILL.md` format (the
portable package format used by ACP-aware clients), while the local MCP tools
provide progressive disclosure and execution. The marketplace adapter is
deliberately configurable through `SKILLS_MARKETPLACE_URL`; the default is
SkillsMP's catalog API and installs are limited to GitHub-backed repositories
so the source remains reviewable before an agent receives the assignment. Large
archives are accepted by default; set `SKILLS_MAX_DOWNLOAD_MB` and/or
`SKILLS_MAX_UNCOMPRESSED_MB` to a positive value when a deployment wants an
explicit cap (`0` means unlimited).

The MCP server is a separate local process. Native Agents SDK agents and Codex sessions receive its tools directly. Gemini uses a guarded OpenAI-compatible function-call bridge for inter-agent messaging and falls back to text-only inference when a model does not support tools; all providers receive queued messages in their provider-specific next prompt.
