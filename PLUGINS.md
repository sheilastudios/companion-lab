# Plugins

A plugin is a program in any language. The lab starts it once per operation, writes one JSON object to stdin, and reads one JSON object from stdout. Exit nonzero to report a failure.

`lab.config.json` picks one of each kind:

```json
{
 "context": "plugins/turns",
 "model": {
  "command": ["python", "-X", "utf8", "-B", "models/openai_compatible/adapter.py"],
  "options": {"base_url": "https://api.openai.com/v1", "api_key_env": "OPENAI_API_KEY", "models": ["your-model"]},
  "models": ["your-model"],
  "default_model": "your-model"
 }
}
```

## Context plugins

A context plugin decides what the model is sent. It lives in a folder with a `plugin.json`:

| Field | Meaning |
|---|---|
| `id` | Its name |
| `command` | How to run it. `python` means the Python running the lab |
| `stateful` | `true` if it keeps its own state and needs the checkpoint operations |
| `roles` | The roles a prompt part can take. Your plugin decides what each means. The basic plugin's are the message roles `system`, `assistant` and `user` |
| `positions` | Optional. Where a part can go, as `[value, label]` pairs; the first is the default, and each part then carries a `position`. The basic plugin's are `start` (before the conversation) and `late` (just before the newest message) |
| `settings` | Extra controls for the Lab drawer: `select`, `text` or `bool` |
| `labels` | Optional wording for the page: `them`, `thinking`, `silence` |
| `parts_note` | Optional explanation shown above the parts |
| `view_label`, `view_title` | Optional. Adds a button that shows your plugin's state |
| `display` | Optional script that can set `window.labDisplay` to `{label, hint, transform(text), viewExtra(view)}`: a display-only filter, and extra text under the view panel |
| `seed` | JSON file holding the starting prompt |
| `imports` | Optional. `["character_card"]` turns on Import character card; the plugin then implements `import_card` (below) |
| `exports` | Optional. `["character_card"]` turns on Export character card; the plugin then implements `export_card` (below) |
| `scripted` | Optional. For the built-in scripted test model: `fixed` values to set in its reply, and `text_field`, the string field that carries its test marker |

Every operation receives `{"op": name, "args": {...}}`. `args` includes `data`, a folder you can use, and usually `session`, which holds `id`, `model`, `effort`, `config` and `turns`.

| Operation | Args | Returns |
|---|---|---|
| `assemble` | `session`, `message`, `request_id`, `budget` | `{"request": {"model": ..., "messages": [...]}}` |
| `commit` | `session`, `request_id`, `raw`, `message` | `{"ok": true, "text": ..., "meta": {"labels": [...]}}` or `{"ok": false, "reason": ...}` |
| `preview` | `config` | `{"text": ...}` |
| `view` (if `view_label`) | `session` | `{"records": [{"turn": 1, "text": ...}], "stored": 1}` |
| `import_card` (if `imports` has `character_card`) | `card`: `{spec, file, fields}`, the card's fields as read from JSON or PNG | `{"config": {"parts": [...], ...}, "notes": [...]}`, loaded into the editor unsaved |
| `export_card` (if `exports` has `character_card`) | `config`, `name` | `{"card": {"spec": "chara_card_v2", "data": {...}}, "notes": [...]}`, downloaded by the page |

The core keeps the turn list, so a plugin with `"stateful": false` needs only `assemble`, `commit` and `preview`. Side by side works only with a plugin that has no state of its own. A stateful one also implements:

| Operation | Args | Returns |
|---|---|---|
| `create` | `session` | `{"state": {...}}`, merged into the session |
| `checkpoint` | `session`, `index` | Saves state from before turn `index` |
| `drop` | `session`, `index`, `keep` | Saves current state under `keep`, then returns to checkpoint `index` |
| `has_kept` | `session`, `keep` | `{"ok": true}` if that saved state exists |
| `undrop` | `session`, `index`, `count`, `keep` | Puts the saved state back |

If an operation fails, the lab stops and shows the error. It doesn't fall back to another plugin, because then you'd be measuring something you didn't ask for.

## Model adapters

An adapter sends a request to a model.

- `{"op": "discover", "options": ...}` returns `{"ok": true, "models": [...]}`.
- `{"op": "chat", "request": ..., "effort": ..., "options": ...}` returns `{"ok": true, "content": ...}` or `{"ok": false, "error": ...}`.

The lab saves the context plugin's request before the adapter sees it, and keeps the adapter's whole reply beside it. An adapter that adds to the request should say what it added in its reply, as the compatible adapter does with `"addedToRequest"` (its reasoning effort), so the two saved files together show what reached the model. The Codex adapter translates the request into Codex's own protocol rather than sending it as is.

The lab records each attempt. It retries only a failure carrying the boolean `"retry": true`; timeouts, unreadable replies, and failures with no explicit retry permission are not retried. A failure may also carry `"detail"`, a sentence to show the person. Return `"retry": false` for exhausted limits and uncertain outcomes. A successful adapter must exit zero and return the boolean `"ok": true` with string `content`.

The compatible adapter refuses tool requests, filtered or truncated replies, and empty/non-text output. This lab does not execute model tools. The Codex adapter checks the running server's MCP inventory before starting a thread; an exposed tool or unreadable inventory refuses the request.

## Interrupted stateful operations

Create, checkpoint, commit, drop and restore cross two stores: the plugin's and the lab's. They are not one atomic transaction. The lab writes `data/pending/<session-id>.json` before changing a stateful conversation and removes it only after the operation and session save succeed. A failure keeps that record and prevents further changes to that conversation, including after a restart. Other conversations and read-only inspection remain available.

A failed `create` has no conversation to hold: the record is kept with the new session's id and the plugin's `create` input, the conversation is not saved, and the page lists it as an unfinished creation, so the plugin's owner can find any state the plugin made for it.

Model replies and observed commit results are kept beside each request as `<request-id>-reply.json` and `<request-id>-commit.json`. A missing commit result does **not** prove that the plugin did nothing. The lab cannot determine how an arbitrary plugin should recover its own state. Stop the lab, back up both stores, and have the plugin's owner reconcile the session with the plugin's state before retiring the pending record. Removing the record alone is not recovery. A failed stateful model attempt is held conservatively too, because the lab cannot infer what the plugin's earlier operations changed.

Plugin `meta` may add fields such as `labels`, but must not replace the lab's core turn fields (`user`, `reply`, `requestId`, `commit`, `prompt`, `parts`, `effort`, `seconds`, `words`, `attempts`, `at`). Invalid metadata is refused and, for a stateful operation, kept behind the recovery hold.

The `state` returned by `create` must likewise add plugin-owned fields, without replacing core session fields such as `id`, `config` or `turns`.

## Scripted and HTTP tests

`POST /api/new` accepts the API-only model `scripted`. It generates a marked test response locally and never starts a model adapter. This works with the shipped turns plugin; a structured plugin can provide the `scripted` schema hints above. It tests plumbing, not memory quality or character behavior.

The page sends `basis: {turns: [requestIds], discarded: [stateIds], prompt, effort}` with a session change. A stale basis is refused before calling the plugin. API clients should send it too; older clients that omit it remain supported but do not get this stale-view protection.

For `/api/turn`, a client can supply `requestId`, 32 lowercase hexadecimal characters. Repeating the same completed request with the same message reads back the session without another model call. Reusing it for different words or a dropped turn is refused. A saved request without a completed turn is held for inspection, not dispatched again. The page uses this identity to recognize a saved Send after a lost HTTP reply. There is no claim of provider-side idempotency or automatic recovery of an uncertain plugin commit.

Two adapters are included:

- **`models/openai_compatible`**, for OpenAI and compatible endpoints.
- **`models/codex`**, which runs your own installed and signed-in `codex app-server` and never reads or extracts tokens. Use your own account, and check that your use fits OpenAI's current terms. It requires a non-empty system prompt, and it doesn't accept the basic plugin's parts placed just before the newest message. Requires Node 18 or later.

```json
"model": {
 "command": ["node", "adapter.mjs"],
 "cwd": "models/codex",
 "models": ["your-model"],
 "efforts": ["medium", "high"],
 "default_effort": "medium",
 "require_auth": "chatgpt"
}
```

`require_auth` keeps the lab on that sign-in type, so it won't use a discovered model from a different kind of account.
