// Run the shipped page's handlers with delayed replies. No browser, network, or external dependencies.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import test from 'node:test';

const html = fs.readFileSync(new URL('../lab.html', import.meta.url), 'utf8');
const script = html.match(/<script>([\s\S]*)<\/script>/)[1];

function page() {
  const elements = new Map();
  function element() {
    return { value: '', textContent: '', classList: { toggle() {} }, style: {}, append() {},
      replaceChildren() {}, addEventListener() {}, focus() {}, add() {}, showModal() {}, close() {} };
  }
  const context = vm.createContext({
    document: { getElementById(id) { if (!elements.has(id)) elements.set(id, element()); return elements.get(id); },
      createElement: element, querySelectorAll() { return []; } },
    localStorage: { getItem() { return null; }, setItem() {} },
    window: { addEventListener() {} }, confirm: () => true, console,
    crypto: { randomUUID: () => 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa' },
  });
  // Suppress only automatic startup fetch; all event handlers are the release's own code.
  vm.runInContext(script.slice(0, script.lastIndexOf('refresh().then(')), context);
  vm.runInContext("C={parts:[{name:'Part',text:'old'}],note:''}; SAVED=JSON.stringify(C); renderCfg=()=>{}; renderStatus=()=>{}; refresh=async()=>{};", context);
  return { context, elements, run(code) { return vm.runInContext(code, context); } };
}

function delayed(p) {
  let resolve;
  p.context.waiting = new Promise(done => { resolve = done; });
  p.run('api=()=>waiting');
  return resolve;
}

test('a save reply cannot replace edits typed while it was in flight', async () => {
  const p = page(), resolve = delayed(p);
  const save = p.elements.get('save').onclick();
  p.run("C.parts[0].text='newer';");
  p.elements.get('note').value = 'newer note';
  resolve({ config: { parts: [{ name: 'Part', text: 'old' }], note: '' } });
  await save;
  assert.equal(p.run('C.parts[0].text'), 'newer');
  assert.equal(p.elements.get('note').value, 'newer note');
  assert.notEqual(p.run('JSON.stringify(C)'), p.run('SAVED'));
});

test('an unchanged editor receives the saved normalized prompt', async () => {
  const p = page(), resolve = delayed(p);
  const save = p.elements.get('save').onclick();
  resolve({ config: { parts: [{ name: 'Part', text: 'old' }], note: 'saved' } });
  await save;
  assert.equal(p.run('C.note'), 'saved');
  assert.equal(p.run('JSON.stringify(C)'), p.run('SAVED'));
});

test('a version reply cannot replace edits typed while it was in flight', async () => {
  const p = page(), resolve = delayed(p);
  p.elements.get('versions').value = 'version.json';
  const load = p.elements.get('versions').onchange();
  p.run("C.parts[0].text='newer';");
  resolve({ config: { parts: [{ name: 'Part', text: 'version' }], note: '' } });
  await load;
  assert.equal(p.run('C.parts[0].text'), 'newer');
});

test('a second save cannot overtake the first', async () => {
  const p = page();
  let calls = 0, resolve;
  p.context.reply = new Promise(done => { resolve = done; });
  p.context.count = () => { calls++; return p.context.reply; };
  p.run('api=count');
  const first = p.elements.get('save').onclick();
  const second = p.elements.get('save').onclick();
  resolve({ config: { parts: [{ name: 'Part', text: 'old' }], note: '' } });
  await Promise.all([first, second]);
  assert.equal(calls, 1);
});

test('lost Send reply is completed only by an exact saved request', async () => {
  for (const requestId of ['a'.repeat(32), 'b'.repeat(32)]) {
    const p = page();
    p.run("S={id:'session',turns:[],discarded:[],prompt:'p',effort:'default'};renderChat=()=>{};");
    p.context.transport = async (path, body) => {
      if (path === '/api/turn') {
        assert.equal(body.requestId, 'a'.repeat(32));
        assert.deepEqual(JSON.parse(JSON.stringify(body.basis)), { turns: [], discarded: [], prompt: 'p', effort: 'default' });
        throw Error('lost reply');
      }
      return { id: 'session', turns: [{ requestId, user: 'same words' }], discarded: [] };
    };
    p.run('api=transport');
    const ok = await p.run("act('/api/turn',{message:'same words'})");
    assert.equal(ok, requestId === 'a'.repeat(32));
  }
});

test('a plugin without optional settings can render its editor', () => {
  const p = page();
  p.run('P={roles:["system"]}; renderSettings();');
});
