// Model adapter for a locally installed, signed-in Codex CLI. It runs your own `codex app-server`; it never reads or extracts tokens.
// Protocol: {"op": "discover"} or {"op": "chat", "request": ..., "effort": ...} on stdin; one JSON object on stdout.
// Codex requires a non-empty first system message and no later system messages, so the turn plugin's "late" role is refused here.
import { discover, codexTransport } from './transport.mjs';
import { usageLimit } from './usage_limit.mjs';

let input = '';
let executable = 'codex';
for await (const chunk of process.stdin) input += chunk;
try {
  const x = JSON.parse(input);
  executable = x.options?.executable ?? 'codex';
  if (x.op === 'discover') {
    const d = await discover({ executable });
    process.stdout.write(JSON.stringify({ ok: true, authType: d.authType, models: d.models.map(m => m.model || m.id) }));
  } else if (x.op === 'chat') {
    const invoke = codexTransport({ executable, effort: x.effort, timeoutMs: x.options?.timeout_ms ?? 600000 });
    const r = await invoke(x.request);
    process.stdout.write(JSON.stringify({ ok: r.outcome === 'content', content: r.content, error: r.outcome === 'content' ? undefined : 'NO_CONTENT', retry: false }));
  } else throw Error('UNKNOWN_OPERATION');
} catch (e) {
  const limit = e.code === 'CODEX_TURN_NOT_COMPLETED' ? await usageLimit({ executable }) : null;
  // These come from the request's shape, the sign-in or the isolation checks: trying again gives the same answer, so say so once.
  const FINAL = new Set(['CODEX_BAD_REQUEST', 'CODEX_UNSUPPORTED_SETTING', 'CODEX_SYSTEM_REQUIRED', 'CODEX_LAST_USER_REQUIRED', 'CODEX_UNSUPPORTED_MESSAGE',
    'CODEX_CHATGPT_LOGIN_REQUIRED', 'CODEX_PROVIDER_OVERRIDE_UNSUPPORTED', 'CODEX_MODEL_MISMATCH', 'CODEX_ISOLATION_MISMATCH', 'CODEX_UNEXPECTED_INSTRUCTIONS']);
  process.stdout.write(JSON.stringify(limit ? { ok: false, error: 'CODEX_USAGE_LIMIT', detail: limit, retry: false }
    : FINAL.has(e.code) ? { ok: false, error: e.code, detail: String(e.message || e.code).slice(0, 300), retry: false }
    : { ok: false, error: e.code || e.message, retry: false }));
  process.exitCode = 1;
}
