"""End-to-end test of the lab with the turn plugin and the OpenAI-compatible adapter, against a fake model. No key or network needed.
    python -X utf8 -B tests/test_lab.py
"""
import hashlib, io, json, os, socket, subprocess, sys, tempfile, threading, time, urllib.error, urllib.request, zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SEED = json.loads((ROOT / 'plugins' / 'turns' / 'seed.json').read_text(encoding='utf-8'))['parts']
SYSTEM = '\n\n'.join(p['text'] for p in SEED if p['role'] == 'system' and p.get('position', 'start') == 'start')
results, calls, failing = [], [], [False]


def get_json(url, timeout=60):
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return json.load(response)


def check(name, ok, detail=''):
    results.append(bool(ok))
    print(('ok    ' if ok else 'FAIL  ') + name + ('' if ok else f'   {str(detail)[:300]}'), flush=True)


class FakeModel(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        calls.append(dict(body=body, auth=self.headers.get('Authorization')))
        if failing[0]:
            self.send_response(500 if failing[0] is True else failing[0])
            self.end_headers()
            return
        data = json.dumps(dict(model='fake-model-served', usage=dict(prompt_tokens=11, completion_tokens=2, total_tokens=13),
                               choices=[dict(message=dict(content=f'Reply {len(calls)}'), finish_reason='stop')])).encode()
        self.send_response(200)
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def free_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


def empty_display(base):
    with urllib.request.urlopen(base + '/plugin/display.js', timeout=5) as response:
        return response.read() == b''


def no_retry_check():
    port = free_port()
    with tempfile.TemporaryDirectory() as tmp:
        calls = Path(tmp) / 'calls.txt'
        script = Path(tmp) / 'adapter.py'
        script.write_text(
            'import json, sys\n'
            'x = json.load(sys.stdin)\n'
            "if x['op'] == 'discover':\n"
            "    print(json.dumps(dict(ok=True, models=['fake-model'])))\n"
            'else:\n'
            f"    open({str(calls)!r}, 'a').write('x')\n"
            "    print(json.dumps(dict(ok=False, error='OUT_OF_USAGE', detail='You are out of usage.', retry=False)))\n",
            encoding='utf-8')
        cfg = Path(tmp) / 'config.json'
        cfg.write_text(json.dumps(dict(context='plugins/turns', model=dict(command=[sys.executable, '-B', str(script)], models=['fake-model'], default_model='fake-model', efforts=['default'], default_effort='default', retry_seconds=0))), encoding='utf-8')
        server = subprocess.Popen([sys.executable, '-X', 'utf8', '-B', str(ROOT / 'lab_server.py')], cwd=ROOT, env=dict(os.environ, LAB_CONFIG=str(cfg), LAB_DATA=str(Path(tmp) / 'data'), LAB_PORT=str(port)), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        base = f'http://127.0.0.1:{port}'
        try:
            def post(path, body):
                req = urllib.request.Request(base + path, data=json.dumps(body).encode(), headers={'Content-Type': 'application/json', 'X-Companion-Lab': '1'})
                try:
                    return get_json(req)
                except urllib.error.HTTPError as e:
                    with e:
                        return dict(http_error=e.code, **json.loads(e.read() or b'{}'))
            for _ in range(50):
                try:
                    get_json(base + '/api/state', timeout=2)
                    break
                except Exception:
                    time.sleep(0.2)
            s = post('/api/new', dict(model='fake-model', effort='default'))
            r = post('/api/turn', dict(id=s['id'], message='Hello'))
            check('an adapter saying retry false is called once and its reason is shown', calls.read_text() == 'x' and 'You are out of usage.' in r.get('error', '') and r.get('http_error') == 400, r)
            failure = post('/api/load', dict(id=s['id']))['failures'][0]
            check('the reason is on record', failure['detail'] == 'You are out of usage.' and len(failure['attempts']) == 1, failure)
        finally:
            server.kill()
            server.wait()


def main():
    fake = ThreadingHTTPServer(('127.0.0.1', 0), FakeModel)
    threading.Thread(target=fake.serve_forever, daemon=True).start()
    port = free_port()
    base = f'http://127.0.0.1:{port}'
    with tempfile.TemporaryDirectory() as tmp:
        cfg = Path(tmp) / 'config.json'
        cfg.write_text(json.dumps(dict(context='plugins/turns', model=dict(command=['python', '-X', 'utf8', '-B', 'models/openai_compatible/adapter.py'], options=dict(base_url=f'http://127.0.0.1:{fake.server_address[1]}/v1', api_key_env='LAB_TEST_KEY', models=['fake-model']), models=['fake-model'], attempts=2, retry_seconds=0))), encoding='utf-8')
        server = subprocess.Popen([sys.executable, '-X', 'utf8', '-B', str(ROOT / 'lab_server.py')], cwd=ROOT, env=dict(os.environ, LAB_CONFIG=str(cfg), LAB_DATA=str(Path(tmp) / 'data'), LAB_PORT=str(port), LAB_TEST_KEY='test-key-value'), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            def post(path, body):
                req = urllib.request.Request(base + path, data=json.dumps(body).encode(), headers={'Content-Type': 'application/json', 'X-Companion-Lab': '1'})
                try:
                    return get_json(req)
                except urllib.error.HTTPError as e:
                    with e:
                        return dict(http_error=e.code, **json.loads(e.read() or b'{}'))

            for _ in range(50):
                time.sleep(0.2)
                try:
                    state = get_json(base + '/api/state', timeout=2)
                    break
                except Exception:
                    pass
            check('server starts with the turn plugin', state['plugin']['id'] == 'turns' and state['models'] == ['fake-model'], state)
            check('no view button and no display script for this plugin', not state['plugin']['view_label'] and empty_display(base))
            s = post('/api/new', dict(model='fake-model', effort='default'))
            sid = s['id']
            s = post('/api/turn', dict(id=sid, message='Hello'))
            check('turn stored', len(s['turns']) == 1 and s['turns'][0]['reply'] == 'Reply 1', s)
            check('key comes from the environment and is not in what the lab records', calls[-1]['auth'] == 'Bearer test-key-value' and 'test-key-value' not in json.dumps(s))
            s = post('/api/turn', dict(id=sid, message='Again'))
            sent = post('/api/sent', dict(id=sid, index=1))
            want = [dict(role='system', content=SYSTEM), dict(role='user', content='Hello'), dict(role='assistant', content='Reply 1'), dict(role='user', content='Again')]
            check('standard turns: full history in order', sent['messages'] == want and calls[-1]['body']['messages'] == want, sent)
            check('what was sent matches what the model received', sent == calls[-1]['body'], sent)
            first = s['turns'][1]['reply']
            s = post('/api/regenerate', dict(id=sid))
            check('regenerate replaces the last turn and keeps the old one', len(s['turns']) == 2 and s['turns'][1]['reply'] != first and s['discarded'][0]['turns'][0]['reply'] == first, s)
            s = post('/api/restore', dict(id=sid, index=0))
            check('restore swaps the dropped turn back', s['turns'][1]['reply'] == first and len(s['discarded']) == 1, s)
            s = post('/api/rewind', dict(id=sid, index=0))
            check('rewind drops everything from a turn on', s['turns'] == [] and len(s['discarded']) == 2, s)
            s = post('/api/turn', dict(id=sid, message='A different start'))
            stale = post('/api/restore', dict(id=sid, index=0))
            check('restore is refused when earlier turns changed', stale.get('http_error') == 400, stale)
            config = state['config']
            check('every part has a message role and a position', all(p['role'] in ('system', 'assistant', 'user') and p['position'] in ('start', 'late') for p in config['parts']), config)
            config['parts'] += [dict(name='Before', role='assistant', position='start', enabled=True, text='PREFACE', source=''),
                                dict(name='After', role='system', position='late', enabled=True, text='LATE', source='')]
            config['note'] = 'placement test'
            saved = post('/api/prompt/save', dict(config=config))['config']
            check('saved prompt keeps its parts and a version', len(saved['parts']) == len(SEED) + 2 and len(get_json(base + '/api/state')['history']) == 2)
            s2 = post('/api/new', dict(model='fake-model', effort='default'))
            post('/api/turn', dict(id=s2['id'], message='Placed'))
            roles = [(m['role'], m['content']) for m in calls[-1]['body']['messages']]
            check('parts go by role and position: system parts joined at the top, then the others before the conversation, then late ones',
                  roles == [('system', SYSTEM), ('assistant', 'PREFACE'), ('system', 'LATE'), ('user', 'Placed')], roles)
            mixed = dict(config, parts=config['parts'] + [dict(name='Aside', role='user', position='late', enabled=True, text='ASIDE', source=''),
                                                          dict(name='Nudge', role='assistant', position='late', enabled=True, text='NUDGE', source='')])
            post('/api/prompt/save', dict(config=mixed))
            s3 = post('/api/new', dict(model='fake-model', effort='default'))
            post('/api/turn', dict(id=s3['id'], message='Placed'))
            roles = [(m['role'], m['content']) for m in calls[-1]['body']['messages']]
            check('late parts keep their own roles and order, each its own message',
                  roles == [('system', SYSTEM), ('assistant', 'PREFACE'), ('system', 'LATE'), ('user', 'ASIDE'), ('assistant', 'NUDGE'), ('user', 'Placed')], roles)
            no_pos = post('/api/prompt/save', dict(config=dict(config, parts=[dict(name='Bare', role='system', enabled=True, text='x', source='')])))['config']
            check('a part saved without a position gets the first one', no_pos['parts'][0].get('position') == 'start', no_pos)
            bad_pos = post('/api/prompt/save', dict(config=dict(config, parts=[dict(name='x', role='system', position='middle', enabled=True, text='x')])))
            check('an unknown position is refused', bad_pos.get('http_error') == 400, bad_pos)
            post('/api/prompt/save', dict(config=saved))

            # Character cards: a V2 card as JSON, a V1 card, and a V2 card inside a PNG.
            import base64, struct, zlib
            enc = lambda raw: base64.b64encode(raw).decode()
            before = get_json(base + '/api/state')
            v2 = dict(spec='chara_card_v2', spec_version='2.0', data=dict(
                name='Marlow', description='{{char}} runs the ferry.', personality='Dry.', scenario='A crossing at dusk.', first_mes='Fare, {{user}}?',
                mes_example='<START>\n{{user}}: Hi\n{{char}}: Hm.', system_prompt='', post_history_instructions='Keep it short.',
                alternate_greetings=['Evening.'], character_book=dict(entries=[dict(keys=['ferry'], content='x')]), creator_notes=''))
            r = post('/api/import_card', dict(name='marlow.json', data=enc(json.dumps(v2).encode())))
            got = {(p['role'], p.get('position'), p['name']): p for p in r.get('config', {}).get('parts', [])}
            check('a V2 card maps onto parts, with {{char}} and {{user}} filled in',
                  got.get(('system', 'start', 'Description'), {}).get('text') == 'Marlow runs the ferry.' and got.get(('system', 'start', 'Personality'), {}).get('text') == 'Dry.'
                  and got.get(('system', 'start', 'Scenario'), {}).get('text') == 'A crossing at dusk.' and got.get(('assistant', 'start', 'First message'), {}).get('text') == 'Fare, the user?'
                  and got.get(('system', 'late', 'Instructions after the conversation'), {}).get('text') == 'Keep it short.', r)
            check('an empty system prompt gets a short default naming the character', 'Marlow' in got.get(('system', 'start', 'System prompt'), {}).get('text', ''), r)
            check('example dialogue comes in switched off', got.get(('assistant', 'start', 'Example dialogue (off: switch on to try it)'), {}).get('enabled') is False, r)
            check('the lorebook and alternate greetings are reported, not imported',
                  any('lorebook (1 entries)' in n for n in r['notes']) and any('1 alternate greetings' in n for n in r['notes']), r)
            after = get_json(base + '/api/state')
            check('importing saves nothing: the prompt and its versions are unchanged', after['config'] == before['config'] and after['history'] == before['history'])
            v1 = post('/api/import_card', dict(name='ivo.json', data=enc(json.dumps(dict(name='Ivo', description='A clockmaker.', first_mes='Tick.')).encode())))
            check('a V1 card (flat fields) imports', [p['text'] for p in v1['config']['parts'] if p['name'] == 'Description'] == ['A clockmaker.'], v1)
            out = post('/api/export_card', dict(config=r['config'], name='Marlow'))
            od = out.get('card', {}).get('data', {})
            check('an imported card exports back to its fields, with {{char}} and {{user}} restored',
                  out.get('card', {}).get('spec') == 'chara_card_v2' and od.get('name') == 'Marlow' and od.get('description') == '{{char}} runs the ferry.'
                  and od.get('personality') == 'Dry.' and od.get('scenario') == 'A crossing at dusk.' and od.get('first_mes') == 'Fare, {{user}}?'
                  and od.get('mes_example') == '<START>\n{{user}}: Hi\n{{char}}: Hm.' and od.get('post_history_instructions') == 'Keep it short.', out)
            again = post('/api/import_card', dict(name='marlow-again.json', data=enc(json.dumps(out['card']).encode())))
            check('and that card imports again to the same parts', [(p['name'], p['text']) for p in again['config']['parts']] == [(p['name'], p['text']) for p in r['config']['parts']], again)
            wren = post('/api/export_card', dict(config=before['config'], name='Wren'))
            check('named parts with no card field join the description', all(t.replace('Wren', '{{char}}') in wren['card']['data']['description']
                  for t in [p['text'] for p in before['config']['parts'] if p['name'] in ('Who she is', 'Last winter', 'Her sister')]), wren)
            aside = dict(before['config'], parts=before['config']['parts'] + [dict(name='Aside', role='user', position='late', enabled=True, text='x', source='')])
            noted = post('/api/export_card', dict(config=aside, name='Wren'))
            check('a part with no card field is left out and named in the notes', any('Aside' in n for n in noted.get('notes', [])), noted)
            check('exporting saves nothing', get_json(base + '/api/state')['history'] == before['history'])

            def chunk(kind, data):
                return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data) & 0xffffffff)
            png = (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', 1, 1, 8, 2, 0, 0, 0))
                   + chunk(b'tEXt', b'chara\0' + base64.b64encode(json.dumps(v2).encode())) + chunk(b'IEND', b''))
            p = post('/api/import_card', dict(name='marlow.png', data=enc(png)))
            check('a card inside a PNG imports the same', [x['text'] for x in p['config']['parts']] == [x['text'] for x in r['config']['parts']], p)
            bare = post('/api/import_card', dict(name='plain.png', data=enc(b'\x89PNG\r\n\x1a\n' + chunk(b'IEND', b''))))
            check('a PNG without a card is refused', bare.get('http_error') == 400 and 'no character card' in bare.get('error', ''), bare)
            junk = post('/api/import_card', dict(name='x.json', data=enc(b'{"hello": 1}')))
            check('JSON that is not a card is refused', junk.get('http_error') == 400, junk)

            # Side by side: two saved versions, one message, nothing added to any conversation.
            h = after['history']
            # Two versions that really differ: identical saves in the same second sort out of order (a "-2" name sorts first), so
            # "the last two" could be one prompt twice, depending on timing.
            latest = post('/api/prompt/version', dict(name=h[-1]))['config']['parts']
            other = next(n for n in reversed(h[:-1]) if post('/api/prompt/version', dict(name=n))['config']['parts'] != latest)
            n0 = len(calls)
            cmp_ = post('/api/compare', dict(a=h[-1], b=other, message='Compare me', model='fake-model', effort='default'))
            check('side by side returns both replies', cmp_.get('a', {}).get('text', '').startswith('Reply') and cmp_.get('b', {}).get('text', '').startswith('Reply')
                  and len(calls) == n0 + 2, cmp_)
            sent_a = [c['body']['messages'] for c in calls[n0:]]
            check('each side was sent fresh, ending with the message', all(m[-1] == dict(role='user', content='Compare me') and not any(x['content'].startswith('Reply') for x in m) for m in sent_a), sent_a)
            check('the two sides used their own versions', sent_a[0] != sent_a[1], sent_a)
            data_dir = Path(tmp) / 'data' / 'compares' / cmp_['id']
            check('both requests and the result are kept', (data_dir / 'request-a.json').exists() and (data_dir / 'request-b.json').exists() and (data_dir / 'result.json').exists())

            # The comparison packet: one zip with the kept files unchanged, both prompt versions as used, and packet.json.
            def post_raw(body):
                req = urllib.request.Request(base + '/api/compare/export', data=json.dumps(body).encode(), headers={'Content-Type': 'application/json', 'X-Companion-Lab': '1'})
                try:
                    with urllib.request.urlopen(req, timeout=30) as r:
                        return r.status, r.headers, r.read()
                except urllib.error.HTTPError as e:
                    with e:
                        return e.code, e.headers, e.read()
            status, headers, body = post_raw(dict(id=cmp_['id']))
            z = zipfile.ZipFile(io.BytesIO(body)) if status == 200 else None
            names = sorted(n.split('/', 1)[1] for n in z.namelist()) if z else []
            check('the packet downloads as a zip', status == 200 and headers.get('Content-Type') == 'application/zip'
                  and f"companion-lab-compare-{cmp_['id']}.zip" in headers.get('Content-Disposition', ''), (status, body[:200]))
            check('it holds both requests, replies and prompt versions, the result and packet.json',
                  names == ['packet.json', 'prompt-a.json', 'prompt-b.json', 'reply-a.json', 'reply-b.json', 'request-a.json', 'request-b.json', 'result.json'], names)
            if z:
                kept_same = all(z.read(f"{cmp_['id']}/{n}") == (data_dir / n).read_bytes() for n in names if n != 'packet.json')
                check('the kept files go in unchanged', kept_same)
                pk = json.loads(z.read(f"{cmp_['id']}/packet.json"))
                check('packet.json names each side: its version, prompt digest, request and reply',
                      pk['sides']['a']['version'] == h[-1] and pk['sides']['b']['version'] == other and pk['sides']['a']['request'] == 'request-a.json'
                      and pk['sides']['a']['prompt'] == cmp_['a']['prompt'] and pk['plugin'] == 'turns', pk.get('sides'))
                check('packet.json carries completion status, served model and token counts',
                      all(pk['sides'][x]['completed'] and pk['sides'][x]['adapter']['finish_reason'] == 'stop' and pk['sides'][x]['adapter']['model'] == 'fake-model-served'
                          and pk['sides'][x]['adapter']['usage']['total_tokens'] == 13 for x in 'ab'), pk.get('sides'))
                check('the prompt versions in the packet are the ones each side used',
                      json.loads(z.read(f"{cmp_['id']}/prompt-b.json"))['parts'] == post('/api/prompt/version', dict(name=other))['config']['parts'])
                check('packet.json lists every file with its SHA-256', all(pk['files'][n] == hashlib.sha256(z.read(f"{cmp_['id']}/{n}")).hexdigest() for n in names if n != 'packet.json')
                      and 'lab_server.py' in pk['code'] and 'models/openai_compatible/adapter.py' in pk['code'], pk.get('code'))
                whole = b''.join(z.read(n) for n in z.namelist())
                check('the endpoint address and key are not in the packet', b'127.0.0.1' not in whole and b'LAB_TEST_KEY' not in whole and b'test-key-value' not in whole)
            bad_id = post_raw(dict(id='../sessions'))
            check('an invalid comparison id is refused', bad_id[0] == 400, bad_id)
            gone = post_raw(dict(id='20200101-000000-abcdef'))
            check('an unknown comparison is not found, naming no folder', gone[0] == 404 and b'compares' not in gone[2], gone)
            kept = json.loads((Path(tmp) / 'data' / 'sessions' / (sid + '.json')).read_text(encoding='utf-8'))['turns']
            n1 = len(calls)
            cont = post('/api/compare', dict(a=h[-1], b=h[-1], message='Go on', model='fake-model', effort='default', id=sid))
            after_turns = json.loads((Path(tmp) / 'data' / 'sessions' / (sid + '.json')).read_text(encoding='utf-8'))['turns']
            check('continuing sends the conversation first, and adds nothing to it',
                  cont.get('history_turns') == len(kept) and all(any(x['content'] == kept[0]['user'] for x in c['body']['messages']) for c in calls[n1:]) and after_turns == kept, cont)
            bad_model = post('/api/compare', dict(a=h[-1], b=h[-2], message='x', model='not-a-model', effort='default'))
            check('an unknown model is refused', bad_model.get('http_error') == 400, bad_model)
            bad_version = post('/api/compare', dict(a='../x.json', b=h[-2], message='x', model='fake-model', effort='default'))
            check('a bad version name is refused', bad_version.get('http_error') == 400, bad_version)
            check('preview shows the assembled prompt', 'PREFACE' in post('/api/preview', dict(config=saved))['text'])
            bad = post('/api/prompt/save', dict(config=dict(parts=[dict(name='x', role='nonsense', text='x')])))
            check('unknown part role is refused', bad.get('http_error') == 400, bad)
            long_message = 'A long pasted page. ' * 150000
            s3 = post('/api/new', dict(model='fake-model', effort='default'))
            r = post('/api/turn', dict(id=s3['id'], message=long_message))
            check('a 3 MB message goes through whole', r.get('turns', [{}])[0].get('user') == long_message.strip() and calls[-1]['body']['messages'][-1]['content'] == long_message.strip(), r.get('error') or r.get('http_error'))
            failing[0] = True
            count = len(calls)
            r = post('/api/turn', dict(id=sid, message='This will fail'))
            failing[0] = False
            check('a failed model call retries, stores nothing and says so', r.get('http_error') == 400 and len(calls) == count + 2 and len(post('/api/load', dict(id=sid))['turns']) == 1, r)
            check('the failure is on record', post('/api/load', dict(id=sid))['failures'][0]['stage'] == 'model')
            failing[0] = 401
            count = len(calls)
            r = post('/api/turn', dict(id=sid, message='Refused'))
            failing[0] = False
            check('a refused request (HTTP 401) is tried once, not retried', r.get('http_error') == 400 and len(calls) == count + 1, r)
            unknown = post('/api/new', dict(model='no-such-model', effort='default'))
            check('a conversation on an unknown model is refused', unknown.get('http_error') == 400 and 'model' in unknown.get('error', ''), unknown)
            hist0 = len(get_json(base + '/api/state')['history'])
            cfg_now = get_json(base + '/api/state')['config']
            post('/api/prompt/save', dict(config=dict(cfg_now, note='first')))
            post('/api/prompt/save', dict(config=dict(cfg_now, note='second')))
            hist = get_json(base + '/api/state')['history']
            check('two saves in the same second are both kept', len(hist) == hist0 + 2 and len(set(hist)) == len(hist), hist[-3:])
            damaged = post('/api/import_card', dict(name='x.png', data=base64.b64encode(b'\x89PNG\r\n\x1a\n' + struct.pack('>I4s', 999999, b'tEXt') + b'short').decode()))
            check('a damaged PNG is refused with a plain reason', damaged.get('http_error') == 400 and 'damaged' in damaged.get('error', ''), damaged)
            garbled = post('/api/import_card', dict(name='x.json', data='not base64!!'))
            check('a file that is not readable is refused with a plain reason', garbled.get('http_error') == 400 and 'could not be read' in garbled.get('error', ''), garbled)
        finally:
            server.kill()
            server.wait()
    fake.shutdown()
    fake.server_close()
    no_retry_check()
    print(f'{sum(results)}/{len(results)} passed')
    return 0 if all(results) else 1


if __name__ == '__main__':
    sys.exit(main())
