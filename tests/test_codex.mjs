// No Codex process or provider: exercise the actual transport with a fake RPC receiver.
import assert from 'node:assert/strict';
import test from 'node:test';
import { codexTransport, mcpToolsAbsent, translate } from '../models/codex/transport.mjs';

const body = { model: 'fake', messages: [{ role: 'system', content: 'A test.' }, { role: 'user', content: 'Hello' }] };

function receiver(inventory, events = []) {
  return class FakeRpc {
    static calls = [];
    listeners = new Set();
    finished = Promise.resolve();
    async initialize() {}
    stop() {}
    async request(method) {
      this.constructor.calls.push(method);
      if (method === 'account/read') return { account: { type: 'chatgpt' } };
      if (method === 'config/read') return { config: {} };
      if (method === 'mcpServerStatus/list') return inventory;
      if (method === 'thread/start') return { model: 'fake', modelProvider: 'openai', approvalPolicy: 'never', sandbox: { type: 'readOnly' }, thread: { id: 'thread-1' } };
      if (method === 'turn/start') {
        // Notifications can precede the RPC acknowledgment.
        for (const event of events) for (const listener of this.listeners) listener(event);
        return { turn: { id: 'turn-1' } };
      }
      throw Error(`Unexpected method ${method}`);
    }
  };
}

for (const [name, inventory, code] of [
  ['tool present', { data: [{ name: 'server', tools: { js: {} } }] }, 'CODEX_MCP_TOOLS_EXPOSED'],
  ['missing rows', {}, 'CODEX_MCP_INVENTORY_UNAVAILABLE'],
  ['missing tools map', { data: [{ name: 'server' }] }, 'CODEX_MCP_INVENTORY_UNAVAILABLE'],
  ['tools array', { data: [{ name: 'server', tools: [] }] }, 'CODEX_MCP_INVENTORY_UNAVAILABLE'],
  ['missing server name', { data: [{ tools: {} }] }, 'CODEX_MCP_INVENTORY_UNAVAILABLE'],
  ['bad pagination', { data: [], nextCursor: 9 }, 'CODEX_MCP_INVENTORY_UNAVAILABLE'],
]) {
  test(`refuse before any thread or turn: ${name}`, async () => {
    const RpcClass = receiver(inventory);
    await assert.rejects(codexTransport({ RpcClass })(body), { code });
    assert(!RpcClass.calls.includes('thread/start'));
    assert(!RpcClass.calls.includes('turn/start'));
  });
}

test('unreadable inventory refuses', async () => {
  await assert.rejects(mcpToolsAbsent({ request() { throw Error('offline'); } }), { code: 'CODEX_MCP_INVENTORY_UNAVAILABLE' });
});

test('pagination checks every page', async () => {
  const pages = [{ data: [], nextCursor: 'next' }, { data: [{ name: 'server', tools: { js: {} } }] }];
  await assert.rejects(mcpToolsAbsent({ async request() { return pages.shift(); } }), { code: 'CODEX_MCP_TOOLS_EXPOSED' });
  assert.equal(pages.length, 0);
});

test('repeated cursor stops', async () => {
  let calls = 0;
  await assert.rejects(mcpToolsAbsent({ async request() { calls++; return { data: [], nextCursor: 'same' }; } }), { code: 'CODEX_MCP_INVENTORY_UNAVAILABLE' });
  assert.equal(calls, 2);
});

test('empty inventories allow a bound early completion, ignoring foreign events', async () => {
  const RpcClass = receiver({ data: [{ name: 'server', tools: {} }] }, [
    { method: 'item/completed', params: { threadId: 'other', turnId: 'turn-1', item: { id: 'foreign', type: 'agentMessage', text: 'Wrong' } } },
    { method: 'item/completed', params: { threadId: 'thread-1', turnId: 'turn-1', item: { id: 'answer', type: 'agentMessage', phase: 'final_answer', text: 'Correct' } } },
    { method: 'turn/completed', params: { threadId: 'thread-1', turn: { id: 'turn-1', status: 'completed' } } },
  ]);
  const result = await codexTransport({ RpcClass })(body);
  assert.equal(result.content, 'Correct');
  assert.equal(RpcClass.calls.filter(m => m === 'turn/start').length, 1);
});

test('unexpected tool item refuses even with an empty MCP inventory', async () => {
  const RpcClass = receiver({ data: [] }, [
    { method: 'item/completed', params: { threadId: 'thread-1', turnId: 'turn-1', item: { id: 'tool', type: 'commandExecution' } } },
  ]);
  await assert.rejects(codexTransport({ RpcClass })(body), { code: 'CODEX_UNEXPECTED_TOOL_OR_ITEM' });
});

test('late system parts are rejected before starting a server', () => {
  assert.throws(() => translate({ ...body, messages: [body.messages[0], { role: 'system', content: 'Late' }, body.messages[1]] }), { code: 'CODEX_UNSUPPORTED_MESSAGE' });
});
