# Companion Lab

A local lab for finding out what your prompts and your memory system actually do.

**What this is:** the public version of the lab we use to build companions. It ships with a basic plugin that sends standard chat turns. **What it isn't:** our companion system. Our context plugin, prompts and memory architecture are proprietary and aren't included. The included Helena transcript was produced in Companion Lab without our proprietary context plugin; it is not a demonstration of the private HearthStage runtime.

## Built while developing HearthStage

This lab helped us develop and evaluate the companions for **HearthStage**,
Sheila Studios' upcoming platform for shared stories, roleplay, and adventures
with AI companions.

We're releasing it so others can experiment with their own prompts, inspect
interactions, and compare results. The included Helena transcript records an
unscripted interaction, not just selected highlights. James shared the existing
novella *Unthink*, written by Sheila, from concepts by James Mayo. Helena's
responses show her engagement with the work; she did not author the novella.

This release is a standalone development tool, not the full HearthStage
companion runtime. Learn more at [HearthStage](https://hearthstage.com).

## Meet Helena: a few moments from the transcript

Fifty unscripted turns: a manuscript, candid criticism, dinner, and pigeons. These are selected excerpts from Helena's replies, not lines from *Unthink*. Each links to its full exchange.

**Choosing how to read** ([turn 8](transcript-helena-2026-11-18.md#8))

> Let me read the opening without a synopsis first. I want to find out what I notice before you tell me what to look for.

**Critique that distinguishes two different problems** ([turn 11](transcript-helena-2026-11-18.md#11))

> The continuity breaks interrupt my belief in what’s happening. The relationship uncertainty changes what I think the whole book is asking me to want.

**Admitting a gap instead of inventing an answer** ([turn 45](transcript-helena-2026-11-18.md#45))

> No. I don't remember what it was, or who had the matching one.

**And room to be funny** ([turn 50](transcript-helena-2026-11-18.md#50))

> They could fly. That's the part I find damning. They're late, they have wings, and they're still insisting there's time to walk.

The complete transcript includes the uncertainty and missed details as well as the successes. It was produced in Companion Lab without our proprietary context plugin; it is one interaction, not a controlled evaluation or a promise that other configurations reproduce it.

*Excerpt credit: Helena (AI-generated replies), in an unscripted conversation with James, Sheila Studios. These excerpts remain CC BY-NC 4.0, as does the full transcript; the documentation's CC BY licence does not override that restriction.*

## What you get

- **Exact requests.** See precisely what the context plugin handed the model adapter on any turn. The compatible adapter's own additions (reasoning effort) are saved with its reply; the Codex adapter translates the request into Codex's own protocol.
- **Prompt parts.** Switch parts on and off, and change where each one sits. Placement can change results more than wording does.
- **Every version kept.** Each saved prompt has an ID, and each turn records which one wrote it.
- **Regenerate, edit and restore.** Rewinds go through the plugin, so a stateful memory system rewinds with the conversation.
- **Plugins for context and for models.** Bring your own memory system, and compare it to plain turns.

## Characters

The starting example is a character, Wren, built from named parts. Each part is a message: you choose its role (system, assistant or user) and where it goes, either before the conversation or just before the newest message. Try changing a part's role or position, or splitting one part into two, and see what changes.

**Our proprietary companion memory system is not included.** You can bring your own context or memory plugin through the documented interface. For an unscripted conversation produced in Companion Lab without our proprietary plugin, read [Helena: two evenings](transcript-helena-2026-11-18.md). The transcript is not evidence of the private HearthStage memory architecture.

## Run it

It needs Python 3.10 or later and no other packages.

```bash
export OPENAI_API_KEY=your-key
python -X utf8 lab_server.py
```

Then open http://127.0.0.1:8814.

In PowerShell, set the key with `$env:OPENAI_API_KEY = 'your-key'` instead of `export`.

Set the model in `lab.config.json`. For any OpenAI-compatible service or local server, change `base_url`. The key is only ever read from the environment variable you name.

To use your own signed-in Codex CLI instead, see `PLUGINS.md`.

### Free, on your own machine

Run a local model with [Ollama](https://ollama.com) or [LM Studio](https://lmstudio.ai). No key is needed, and the lab lists the models you have installed:

```bash
LAB_CONFIG=examples/lab.config.ollama.json python -X utf8 lab_server.py
LAB_CONFIG=examples/lab.config.lmstudio.json python -X utf8 lab_server.py
```

Ollama serves on port 11434 once it's running. In LM Studio, start the local server first; it uses port 1234.

## Bring a character

In the Lab drawer, **Import character card** reads SillyTavern cards (V1, V2 or V3), as a JSON file or the PNG they usually come in. The card fills the editor as parts, so you can see exactly where each field goes and move things around before you save:

| Card field | Becomes |
|---|---|
| System prompt (or a short default), scenario, description and personality | System parts, before the conversation |
| First message | An assistant part, before the conversation |
| Example dialogue | An assistant part, before the conversation, switched off to start |
| Post-history instructions | A system part, just before the newest message |

`{{char}}` becomes the character's name and `{{user}}` becomes "the user". Lorebooks and alternate greetings aren't imported, and the lab tells you when a card has them.

**Export character card** goes the other way: it saves the prompt in the editor as a V2 card (JSON), with the character's name turned back into `{{char}}`. Named parts go back to their fields; other system parts before the conversation join the description. A card holds less than a prompt can (no user parts, one first message, one example dialogue), so the lab lists every part it left out and why.

## Side by side

In the Lab drawer, **Side by side** sends one message under two saved prompt versions and shows the replies next to each other. Leave it fresh, or tick **continue the open conversation** to send it after that conversation's turns. Nothing is added to the conversation. Both requests and replies are kept in `data/compares/`.

## Test it

```bash
python -X utf8 tests/test_lab.py
```

This runs against a fake model, so it needs no key.

## Write a plugin

See `PLUGINS.md`. Start with `plugins/turns/plugin.py` and its `plugin.json`.

The interface lets you build and test your own stateful memory plugin. This release supplies the lab and measurement tools; it does not supply our memory design, prompts, selectors, or research. The synthetic plugin under `tests/fixtures/` is only a failure-test fixture that remembers test lines.

Stateful plugin changes and transcript saves are separate writes. An interrupted change is held for the plugin owner to reconcile, rather than retried silently. Read the recovery contract in `PLUGINS.md` before using a stateful plugin with valuable data.

Run one lab server per data store. Use a separate `LAB_DATA` directory when testing another context plugin; changing a plugin is not a migration of its saved state.

Additional failure tests use disposable stores and fake receivers:

```bash
python -X utf8 -B -m unittest discover -s tests -p test_pressure.py
node --test tests/test_page.mjs tests/test_codex.mjs
```

Node is needed only for these JavaScript tests and the optional Codex adapter. The page-handler tests simulate delayed responses; they do not replace a browser check. Keep the lab on its loopback address: it is a local development tool, not an internet-facing service.

## Licensing and attribution

The lab code is **MIT-licensed**, including commercial use with its notice
retained. The documentation is **CC BY 4.0**. The Helena transcript and
**Unthink**, including the novella inside that transcript, are **CC BY-NC
4.0**: free noncommercial reading, sharing and adaptation with attribution;
**selling the novella is not permitted by this licence**.

See [licence scopes and suggested credits](LICENSING.md) and the full texts
in [LICENSES](LICENSES/). The code licence does not relicense the literary
material. No private HearthStage runtime or companion memory is included.
