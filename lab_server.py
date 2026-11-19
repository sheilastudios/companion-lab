"""Companion Lab: a local chat for testing prompts, with pluggable context management and model access.
The core keeps conversations, prompt versions and the exact bytes sent. A context plugin decides what gets sent; a model adapter sends it.
See lab.config.json. Run: python -X utf8 lab_server.py
"""
import base64, hashlib, io, json, os, re, struct, subprocess, sys, threading, time, uuid, zipfile, zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONFIG = json.loads(Path(os.environ.get('LAB_CONFIG') or ROOT / 'lab.config.json').read_text(encoding='utf-8'))
DATA = Path(os.environ.get('LAB_DATA') or ROOT / 'data')
SESSIONS, PROMPTS, REQUESTS = DATA / 'sessions', DATA / 'prompts', DATA / 'requests'
PORT = int(os.environ.get('LAB_PORT') or CONFIG.get('port', 8814))
ORIGIN = f'http://127.0.0.1:{PORT}'
PLUGIN_DIR = ROOT / CONFIG['context']
PLUGIN = json.loads((PLUGIN_DIR / 'plugin.json').read_text(encoding='utf-8'))
MODEL = CONFIG['model']
MODEL_DIR = ROOT / MODEL.get('cwd', '.')
EFFORTS = MODEL.get('efforts', ['default'])
MODEL_ATTEMPTS = MODEL.get('attempts', 3)
MODELS = list(MODEL.get('models', []))
STATEFUL = PLUGIN.get('stateful', False)
LOCK = threading.Lock()
FLAGS = getattr(subprocess, 'CREATE_NO_WINDOW', 0)


def sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


def read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def write(path, value):
    """Replace one JSON file; a failed write leaves its previous version intact."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        temp.write_text(json.dumps(value, ensure_ascii=False, indent=1), encoding='utf-8', newline='\n')
        # Windows readers can briefly prevent replacement. Retry only that failure.
        for attempt in range(20):
            try:
                os.replace(temp, path)
                return
            except PermissionError:
                if attempt == 19:
                    raise
                time.sleep(0.05 * (attempt + 1))
    finally:
        temp.unlink(missing_ok=True)


def identity(value):
    if not isinstance(value, str) or not re.fullmatch('[a-f0-9]{32}', value):
        raise ValueError('Invalid identity')
    return value


def command(cmd):
    return [sys.executable if cmd[0] == 'python' else cmd[0], *cmd[1:]]


def clean(config):
    if not isinstance(config, dict) or not isinstance(config.get('parts'), list):
        raise ValueError('Invalid prompt')
    out = {}
    for s in PLUGIN.get('settings', []):
        # A select setting a saved prompt never had stays absent (the plugin defaults it at use), so old saves clean to the
        # same bytes and the same digest as before (parity check, 11-07). A present value must be one of the options.
        value = config.get(s['key'])
        if s['type'] == 'select' and value is None:
            continue
        if s['type'] == 'select' and value not in [o[0] for o in s['options']]:
            raise ValueError(f"Invalid setting: {s['key']}")
        if s['type'] == 'text' and not isinstance(value, str):
            raise ValueError(f"Invalid setting: {s['key']}")
        out[s['key']] = value is True if s['type'] == 'bool' else value
    parts = []
    for p in config['parts']:
        if not isinstance(p, dict) or not isinstance(p.get('text'), str) or p.get('role') not in PLUGIN['roles']:
            raise ValueError('Invalid part')
        # `when`: 'opening' sends the part with turn 1 only (an opening scene); the default, every turn, is left out so older saves keep their bytes and digest (11-07).
        if p.get('when') not in (None, 'always', 'opening'):
            raise ValueError('Invalid part')
        # `position`: only for a plugin that declares positions (plugin.json "positions": [[value, label], ...]); the first is the default.
        # A plugin without them never gets the field, so its saves keep their bytes and digest.
        place = {}
        if PLUGIN.get('positions'):
            allowed = [x[0] for x in PLUGIN['positions']]
            if p.get('position', allowed[0]) not in allowed:
                raise ValueError('Invalid part position')
            place = {'position': p.get('position', allowed[0])}
        parts.append(dict(name=str(p.get('name', 'Part'))[:80], role=p['role'], **place, enabled=p.get('enabled') is True, text=p['text'], source=str(p.get('source', 'added in lab'))[:200], **({'when': 'opening'} if p.get('when') == 'opening' else {})))
    return dict(out, parts=parts, note=str(config.get('note', ''))[:200])


def digest(config):
    return sha(json.dumps({k: v for k, v in config.items() if k != 'note'}, ensure_ascii=False, sort_keys=True))[:12]


def config():
    """The saved prompt. Its shape is checked, so a damaged file gets the readable error instead of a page that cannot render.
    Its roles and settings are not: after switching to a plugin with different roles the lab still opens and shows what to change."""
    value = read(PROMPTS / 'parts.json')
    if not isinstance(value, dict) or not isinstance(value.get('parts'), list) or not all(isinstance(p, dict) and isinstance(p.get('text'), str) for p in value['parts']):
        raise ValueError('The saved prompt is malformed.')
    return value


def save_config(value):
    value = clean(value)
    stamp = time.strftime('%Y%m%d-%H%M%S') + '-' + digest(value)
    # Every save is kept: a second save in the same second (a changed note, a double click) gets its own name, never overwrites.
    name, n = stamp, 1
    while (PROMPTS / 'history' / (name + '.json')).exists():
        n += 1
        name = f'{stamp}-{n}'
    write(PROMPTS / 'history' / (name + '.json'), value)
    write(PROMPTS / 'parts.json', value)
    # A failed hook must not cost the save.
    for hook in CONFIG.get('on_prompt_saved', []):
        try:
            subprocess.Popen(command(hook), cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=FLAGS)
        except OSError as e:
            print(f'Prompt saved. Save hook could not start: {e}', file=sys.stderr)
    return value


def plugin(op, session=None, **args):
    if session is not None:
        args['session'] = {k: v for k, v in session.items() if k not in ('discarded', 'failures')}
    p = subprocess.run(command(PLUGIN['command']), cwd=PLUGIN_DIR, input=json.dumps(dict(op=op, args=dict(args, data=str(DATA))), ensure_ascii=False), capture_output=True, text=True, encoding='utf-8', timeout=CONFIG.get('plugin_timeout_seconds', 300), creationflags=FLAGS)
    if p.returncode:
        raise RuntimeError(f'Context plugin {op} failed: {p.stderr[-600:]}')
    return json.loads(p.stdout)


def adapter(payload, timeout):
    if MODEL.get('options'):
        payload = dict(payload, options=MODEL['options'])
    return subprocess.run(command(MODEL['command']), cwd=MODEL_DIR, input=json.dumps(payload, ensure_ascii=False), capture_output=True, text=True, encoding='utf-8', timeout=timeout, creationflags=FLAGS)


def model_call(request, effort):
    """Retry only an adapter's explicit retry=True, never an ambiguous outcome."""
    last = dict(ok=False, error='NOT_ATTEMPTED')
    attempts = []
    for n in range(1, MODEL_ATTEMPTS + 1):
        try:
            p = adapter(dict(op='chat', request=request, effort=effort), MODEL.get('timeout_seconds', 1830))
            last = json.loads(p.stdout)
            if not isinstance(last, dict):
                raise ValueError('Expected an adapter response object')
            if type(last.get('ok')) is not bool:
                raise ValueError('Expected a boolean adapter status')
            if last['ok'] and (p.returncode or not isinstance(last.get('content'), str)):
                raise ValueError('Expected a text response on adapter success')
        except subprocess.TimeoutExpired:
            last = dict(ok=False, error='ADAPTER_TIMEOUT', retry=False,
                        detail='The model adapter timed out. The turn outcome is uncertain.')
        except OSError:
            last = dict(ok=False, error='ADAPTER_START_FAILED', retry=False,
                        detail='The lab could not start the model adapter.')
        except ValueError:
            last = dict(ok=False, error='ADAPTER_OUTPUT_UNREADABLE', retry=False,
                        detail='The adapter returned an unreadable result. The turn outcome is uncertain.')
        attempts.append(dict(attempt=n, ok=bool(last.get('ok')), error=last.get('error')))
        if last.get('ok') and isinstance(last.get('content'), str):
            break
        if last.get('retry') is not True:
            break
        if n < MODEL_ATTEMPTS:
            time.sleep(MODEL.get('retry_seconds', 5))
    last['attempts'] = attempts
    return last


# An API-only test model. Plumbing tests need no provider or character generation.
# POST /api/new with model "scripted"; never listed on the page or sent to an adapter.
SCRIPTED = 'scripted'
# What a scripted turn says comes from the context plugin (plugin.json "scripted"): `fixed` values to set, and `text_field`, the string field
# that carries the test marker. With none, the lab fills the plugin's schema and marks the first required string field it finds.
SCRIPTED_HINTS = PLUGIN.get('scripted') or {}


def _fill(schema):
    """The smallest value a strict JSON schema accepts: required fields only, empty arrays, first enum value, null where allowed."""
    t = schema.get('type')
    if 'enum' in schema:
        return schema['enum'][0]
    if isinstance(t, list):
        return None if 'null' in t else _fill(dict(schema, type=t[0]))
    if t == 'object':
        return {k: _fill(schema['properties'][k]) for k in schema.get('required', [])}
    return {'array': [], 'string': '', 'number': 0, 'integer': 0, 'boolean': False}.get(t)


def scripted_call(request):
    """A scripted turn for whatever the host asked for: its own response schema, filled; the scene quotes the earliest message from
    the person that this request carries, so a check can see that saved history reached the request. Nothing here is anyone's words."""
    try:
        marker = f'(Scripted test turn {uuid.uuid4().hex[:8]}; no model was called.)'
        if 'response_format' not in request:
            return dict(ok=True, content=marker, attempts=[dict(attempt=1, ok=True, error=None)])
        schema = request['response_format']['json_schema']['schema']
        value = _fill(schema)
        said = [m['content'] for m in request.get('messages', []) if m.get('role') == 'user' and isinstance(m.get('content'), str)]
        first = ' '.join(said[0].split())[-160:] if said else ''
        value.update(SCRIPTED_HINTS.get('fixed') or {})
        field = SCRIPTED_HINTS.get('text_field') or next((k for k in schema.get('required', []) if isinstance(value.get(k), str)), None)
        if field is None:
            raise KeyError('no string field for the scripted text')
        value[field] = f'{marker}\n\nThe earliest message in this request: "{first}"'
        return dict(ok=True, content=json.dumps(value, ensure_ascii=False), attempts=[dict(attempt=1, ok=True, error=None)])
    except (KeyError, TypeError, AttributeError) as e:
        return dict(ok=False, error='SCRIPTED_UNREADABLE_REQUEST', retry=False, detail=f'The scripted model could not read the request ({type(e).__name__}).',
                    attempts=[dict(attempt=1, ok=False, error='SCRIPTED_UNREADABLE_REQUEST')])


CARD_MAX = 20 * 1024 * 1024
REQUEST_MAX = 40 * 1024 * 1024   # a base64 card at CARD_MAX is about 27 MB; a whole novel pasted as a message is far smaller
PNG_MAGIC = b'\x89PNG\r\n\x1a\n'


def _png_text(raw):
    """Text chunks of a PNG ({keyword: text}): tEXt (Latin-1) and iTXt (UTF-8, maybe compressed). Character cards keep their JSON,
    base64-encoded, under 'chara' (V2) or 'ccv3' (V3)."""
    out, i = {}, len(PNG_MAGIC)
    while i + 8 <= len(raw):
        size, kind = struct.unpack('>I4s', raw[i:i + 8])
        if i + 12 + size > len(raw):
            raise ValueError('That PNG is damaged (a chunk runs past the end of the file).')
        body = raw[i + 8:i + 8 + size]
        i += 12 + size
        if kind == b'tEXt' and b'\0' in body:
            k, v = body.split(b'\0', 1)
            out[k.decode('latin-1')] = v.decode('latin-1')
        elif kind == b'iTXt' and b'\0' in body:
            try:
                k, rest = body.split(b'\0', 1)
                compressed, rest = rest[0], rest[2:]
                _lang, rest = rest.split(b'\0', 1)
                _translated, text = rest.split(b'\0', 1)
                if compressed:
                    # Capped: a small compressed chunk must not expand without limit.
                    inflate = zlib.decompressobj()
                    text = inflate.decompress(text, CARD_MAX)
                    if inflate.unconsumed_tail:
                        raise ValueError('That PNG has an oversized text chunk.')
                out[k.decode('latin-1')] = text.decode('utf-8')
            except (IndexError, ValueError, zlib.error) as e:
                if 'oversized' in str(e):
                    raise
                raise ValueError('That PNG has a damaged text chunk.') from None
        elif kind == b'IEND':
            break
    return out


def read_card(name, data):
    """A character card, from a JSON file or a PNG carrying one. Returns {spec, fields}: V2/V3 cards keep their fields under `data`;
    V1 cards are flat. Nothing in the card is run or fetched; it is only read."""
    try:
        raw = base64.b64decode(data, validate=True)
    except (ValueError, TypeError):
        raise ValueError('That file could not be read.') from None
    if len(raw) > CARD_MAX:
        raise ValueError('That file is too large for a character card.')
    try:
        if raw.startswith(PNG_MAGIC):
            chunks = _png_text(raw)
            found = chunks.get('ccv3') or chunks.get('chara')
            if not found:
                raise LookupError
            card = json.loads(base64.b64decode(found).decode('utf-8'))
        else:
            card = json.loads(raw.decode('utf-8-sig'))
    except LookupError:
        raise ValueError('That PNG has no character card in it.') from None
    except (UnicodeDecodeError, json.JSONDecodeError, base64.binascii.Error):
        raise ValueError('That character card could not be read.') from None
    if not isinstance(card, dict):
        raise ValueError('That is not a character card.')
    fields = card['data'] if isinstance(card.get('data'), dict) else card
    if not isinstance(fields.get('name'), str) or not any(isinstance(fields.get(k), str) and fields[k].strip() for k in ('description', 'personality', 'first_mes', 'system_prompt')):
        raise ValueError('That is not a character card (no name and description).')
    return dict(spec=str(card.get('spec') or 'chara_card_v1')[:40], file=str(name)[:120], fields=fields)


def import_card(name, data):
    if 'character_card' not in (PLUGIN.get('imports') or []):
        raise ValueError('This context plugin cannot import character cards.')
    result = plugin('import_card', card=read_card(name, data))
    return dict(config=clean(result['config']), notes=[str(n)[:300] for n in result.get('notes', [])][:20])


def export_card(config, name):
    """The editor's prompt as a character card (JSON), made by the plugin. Nothing is saved; the page downloads it."""
    if 'character_card' not in (PLUGIN.get('exports') or []):
        raise ValueError('This context plugin cannot export character cards.')
    result = plugin('export_card', config=clean(config), name=str(name or '')[:80])
    card = result.get('card')
    if not isinstance(card, dict) or not isinstance(card.get('data'), dict):
        raise ValueError('The plugin did not return a character card.')
    # Room for one note per part plus a heading: an export names every part it leaves out (the cap only bounds a misbehaving plugin).
    return dict(card=card, notes=[str(n)[:300] for n in result.get('notes', [])][:len(config['parts']) + 2])


def compare(b):
    """Side by side: one message sent once under each of two saved prompt versions, fresh or continuing an open conversation's turns.
    Nothing is added to any conversation. Both requests and replies are kept under data/compares/<id>."""
    if STATEFUL:
        raise ValueError('Side by side needs a context plugin without its own state.')
    message = b.get('message').strip() if isinstance(b.get('message'), str) else ''
    if not message:
        raise ValueError('Enter a message')
    if b.get('model') not in MODELS + [SCRIPTED] or b.get('effort') not in EFFORTS:
        raise ValueError('Choose an available model and effort')
    names = [b.get('a'), b.get('b')]
    if not all(isinstance(n, str) and re.fullmatch(r'[0-9a-f-]+\.json', n) for n in names):
        raise ValueError('Choose two saved versions')
    turns = read_session(b['id'])['turns'] if b.get('id') else []
    # Both versions are read before either side runs, so a missing one fails cleanly instead of after a whole model turn.
    cfgs = [read(PROMPTS / 'history' / name) for name in names]
    cid = time.strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:6]
    # Each side's prompt version is kept with the comparison as it was used, so a packet still reproduces it after the version is
    # edited or deleted from the history.
    for i, cfg in enumerate(cfgs):
        write(DATA / 'compares' / cid / f'prompt-{"ab"[i]}.json', cfg)
    out = [None, None]
    # The plugin and adapter run as new processes for each call, so their code is read from disk then. Fingerprint it before the
    # first side and again after the last: if anything changed in between, which version each side ran is uncertain, and the record
    # says so. Taking fingerprints never fails a comparison.
    before = code_snapshot()

    def side(i):
        cfg = cfgs[i]
        started = time.monotonic()
        try:
            sess = dict(id='compare-' + cid, model=b['model'], effort=b['effort'], config=cfg, prompt=digest(cfg), turns=turns)
            request = plugin('assemble', session=sess, message=message, request_id=f'{cid}-{i}', budget=CONFIG.get('token_budget'))['request']
            write(DATA / 'compares' / cid / f'request-{"ab"[i]}.json', request)
            reply = scripted_call(request) if b['model'] == SCRIPTED else model_call(request, b['effort'])
            # The adapter's whole reply, as for a turn: its attempts, and what it added to the request.
            write(DATA / 'compares' / cid / f'reply-{"ab"[i]}.json', reply)
            if not reply.get('ok') or not isinstance(reply.get('content'), str):
                raise RuntimeError(reply.get('detail') or f"The model did not complete ({reply.get('error')}).")
            done = plugin('commit', session=sess, request_id=f'{cid}-{i}', raw=reply['content'], message=message)
            if not done.get('ok'):
                raise RuntimeError(f"The context plugin refused this reply: {done.get('reason')}")
            out[i] = dict(version=names[i], prompt=digest(cfg), text=done['text'], seconds=round(time.monotonic() - started, 1), words=len(done['text'].split()))
        except Exception as e:
            out[i] = dict(version=names[i], prompt=digest(cfg), error=str(e)[-600:], seconds=round(time.monotonic() - started, 1))
    # One side after the other: a model adapter such as the Codex one runs a local app-server against shared state, and two at once
    # (or three with a turn) can collide. Side by side runs under the same lock as a turn.
    side(0)
    side(1)
    after = code_snapshot()
    result = dict(id=cid, at=time.strftime('%Y-%m-%d %H:%M:%S'), message=message, model=b['model'], effort=b['effort'], plugin=PLUGIN.get('id'), continued=b.get('id') or None,
                  history_turns=len(turns), a=out[0], b=out[1], adapter=adapter_id(), **code_record(before, after))
    write(DATA / 'compares' / cid / 'result.json', result)
    return result


CODE_SCOPE = ('lab_server.py as this lab process loaded it; plugin.json and every file in the context plugin\'s folder; and every script '
              'the plugin\'s or model adapter\'s command names, with every file in that script\'s folder (so an adapter\'s own imports '
              'there are covered). Shared code outside those folders, the Python or Node runtime, and the model endpoint are not covered.')
CODE_SKIP = {'__pycache__', 'node_modules', '.git'}
CODE_MAX = 200
# The lab's own code as this process loaded it. Its file on disk can change after the lab starts; the record uses what is running.
LAB_SOURCE_SHA = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
# The current shape of codeStatus. Records without it came from an earlier lab version, and an export treats them conservatively.
CODE_STATUS_SCHEMA = 'code-status/2'


def code_name(f):
    """A portable, unique name for a fingerprinted file: its path relative to the lab, or, for a file elsewhere, external/ plus a short
    hash of its folder and its file name, so two outside files with the same name never collapse and no local path is shown."""
    root = ROOT.resolve()
    if f.is_relative_to(root):
        return f.relative_to(root).as_posix()
    return f'external/{hashlib.sha256(str(f.parent).encode()).hexdigest()[:8]}/{f.name}'


def script_paths():
    """The scripts the plugin's and adapter's commands name, as resolved paths, whether or not they exist now: a named script that is
    missing is part of what the record should have covered."""
    out = []
    for cmd, cwd in ((PLUGIN['command'], PLUGIN_DIR), (MODEL.get('command', []), MODEL_DIR)):
        for a in cmd[1:]:
            if isinstance(a, str) and re.search(r'\.(py|mjs|js)$', a):
                out.append((cwd / a).resolve())
    return out


def adapter_id():
    """The model adapter as a portable name (code_name of its script)."""
    adapter = [(MODEL_DIR / a).resolve() for a in MODEL.get('command', [])[1:] if isinstance(a, str) and re.search(r'\.(py|mjs|js)$', a)]
    return code_name(adapter[-1]) if adapter else None


def code_snapshot():
    """Fingerprints of the plugin and adapter code on disk now, within CODE_SCOPE. Never raises: a file that can't be read is listed
    under unreadable, and files past CODE_MAX are counted under omitted. The command scripts and plugin.json always come first."""
    try:
        root, data = ROOT.resolve(), DATA.resolve()
        expected = list(dict.fromkeys([(PLUGIN_DIR / 'plugin.json').resolve(), *script_paths()]))
        missing = [code_name(p) for p in expected if not p.is_file()]
        required = [p for p in expected if p.is_file()]
        folders = {PLUGIN_DIR.resolve()} | {s.parent for s in expected if s.parent != root and s.parent.is_dir()}
        extra, scan_errors = set(), []
        for folder in folders:
            # os.walk reports a folder it cannot list instead of skipping it silently, so an incomplete scan is known.
            for here, subdirs, names in os.walk(folder, onerror=scan_errors.append):
                subdirs[:] = [d for d in subdirs if d not in CODE_SKIP]
                for name in names:
                    candidate = Path(here) / name
                    if candidate.suffix not in ('.pyc', '.log') and candidate.is_file() and not candidate.resolve().is_relative_to(data):
                        extra.add(candidate.resolve())
        ordered = list(dict.fromkeys(required)) + sorted(extra - set(required))
        limit = max(CODE_MAX, len(required))
        files, unreadable = {}, []
        for f in ordered[:limit]:
            try:
                files[code_name(f)] = hashlib.sha256(f.read_bytes()).hexdigest()
            except OSError:
                unreadable.append(code_name(f))
        return dict(files=files, unreadable=unreadable, missing=missing, omitted=max(0, len(ordered) - limit), scanIncomplete=bool(scan_errors))
    except Exception:
        return dict(files={}, unreadable=[], missing=[], omitted=0, scanIncomplete=True, failed=True)


def code_record(before, after):
    """The code fields saved with a comparison: the fingerprints taken before it ran, and an honest account of how far they go."""
    changed = sorted(n for n in set(before['files']) | set(after['files']) if before['files'].get(n) != after['files'].get(n))
    try:
        lab_on_disk = hashlib.sha256((ROOT / 'lab_server.py').read_bytes()).hexdigest()
    except OSError:
        lab_on_disk = None
    failed = bool(before.get('failed') or after.get('failed'))
    unreadable = sorted(set(before['unreadable']) | set(after['unreadable']))
    omitted = max(before['omitted'], after['omitted'])
    scan_incomplete = bool(before.get('scanIncomplete') or after.get('scanIncomplete'))
    # A script the configuration names, or plugin.json, that was not there to fingerprint.
    missing = sorted(set(before.get('missing', [])) | set(after.get('missing', [])))
    # Two separate questions. stable: nothing in scope changed between the two snapshots. complete: every file in scope was found
    # and read. certain needs both.
    stable = not changed and not failed
    complete = not failed and not unreadable and not missing and not omitted and not scan_incomplete
    status = dict(schema=CODE_STATUS_SCHEMA,
                  taken='Plugin and adapter files: read from disk just before the first side ran, and checked again after the last side. '
                        'lab_server.py: the code this lab process loaded.',
                  changedDuringRun=changed, unreadable=unreadable, missingRequired=missing, omitted=omitted, scanIncomplete=scan_incomplete,
                  labChangedOnDiskSinceStart=lab_on_disk != LAB_SOURCE_SHA, stable=stable, complete=complete, certain=stable and complete)
    notes = []
    if failed:
        notes.append('Fingerprints could not be taken for this comparison.')
    if changed:
        notes.append('Files changed on disk while this comparison ran, so which version each side ran is uncertain. '
                     'The fingerprints are from before the first side.')
    if unreadable or missing or omitted or scan_incomplete:
        notes.append('Not every file in scope was fingerprinted (see unreadable, missingRequired, omitted and scanIncomplete), so the '
                     'record is incomplete.')
    if notes:
        status['note'] = ' '.join(notes)
    return dict(code={'lab_server.py': LAB_SOURCE_SHA, **before['files']}, codeScope=CODE_SCOPE, codeStatus=status)


# A packet carries only the files the lab writes for a comparison, read only from that comparison's own folder.
PACKET_MEMBERS = ('result.json', 'prompt-a.json', 'prompt-b.json', 'request-a.json', 'request-b.json', 'reply-a.json', 'reply-b.json')


# A code is one identifier: letters, digits and underscores. No dots, hyphens, colons or slashes, so a hostname, an address or a
# URL can never pass as one.
CODE_TOKEN = re.compile(r'[A-Za-z_][A-Za-z0-9_]{0,63}')
# What a packet keeps of an adapter's addedToRequest: standard generation settings, each held to its own value contract. Anything
# else an adapter added (routing, authentication, URLs), or a setting whose value breaks its contract, is left out and only counted.
EFFORT_LEVELS = {'none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max', 'default'}
GENERATION_SETTINGS = {
    'reasoning_effort': EFFORT_LEVELS, 'effort': EFFORT_LEVELS, 'verbosity': {'low', 'medium', 'high'},
    'temperature': float, 'top_p': float, 'min_p': float, 'presence_penalty': float, 'frequency_penalty': float, 'repeat_penalty': float,
    'top_k': int, 'max_tokens': int, 'max_completion_tokens': int, 'seed': int, 'n': int,
    'stream': bool,
}


def setting_ok(contract, value):
    """Whether a value meets its setting's contract: one of a fixed set of words, an integer, a number, or a boolean."""
    if isinstance(contract, set):
        return isinstance(value, str) and value in contract
    if contract is bool:
        return type(value) is bool
    if contract is int:
        return type(value) is int
    return type(value) in (int, float)


def generation_settings(added):
    """An adapter's addedToRequest as a packet carries it: GENERATION_SETTINGS that meet their contracts, plus how many were left out."""
    kept = {k: v for k, v in added.items() if k in GENERATION_SETTINGS and setting_ok(GENERATION_SETTINGS[k], v)}
    return kept, len(added) - len(kept)


def error_code(value, depth=0):
    """A diagnostic reduced to a bounded code, whatever its shape. Provider error bodies, exception text and an adapter's structured
    error objects can carry a rejected key or a private address, so a packet never includes them:
    - a string: 'HTTP 401: <body>' becomes 'HTTP 401', 'URLError: <text>' becomes 'URLError', an upper-case code such as
      ADAPTER_TIMEOUT stays as it is, and any other text becomes ERROR;
    - an object: its "code", "type" or "error" field if that is a short code token (or, for "error", reduces to one); else ERROR;
    - anything else (a number, a list): ERROR. None stays None."""
    if value is None:
        return None
    if isinstance(value, dict) and depth < 2:
        for key in ('code', 'type', 'error'):
            v = value.get(key)
            if isinstance(v, str) and CODE_TOKEN.fullmatch(v):
                return v
            if key == 'error' and isinstance(v, (str, dict)):
                return error_code(v, depth + 1)
        return 'ERROR'
    if not isinstance(value, str):
        return 'ERROR'
    if re.fullmatch(r'[A-Z][A-Z0-9_]{0,63}', value):
        return value
    m = re.match(r'(HTTP [0-9]{3})\b', value) or re.match(r'([A-Z]\w{0,63})(?=:)', value, re.ASCII)
    return m.group(1) if m else 'ERROR'


def counts(value, depth=0):
    """Token usage as a packet carries it: numbers only, one nested level (as in prompt_tokens_details)."""
    if not isinstance(value, dict):
        return None
    out = {}
    for k, amount in value.items():
        if not isinstance(k, str) or not CODE_TOKEN.fullmatch(k):
            continue
        if type(amount) in (int, float):
            out[k] = amount
        elif isinstance(amount, dict) and depth == 0:
            out[k] = counts(amount, 1)
    return out


def model_name(value):
    """The model an endpoint says served the call, as a bare name. A local server can report a file path, so only its last part is
    kept. A value that looks like an address (an IP address, a host:port, user@host, or a URL) becomes OTHER. Anything else is kept as
    the endpoint reported it, so a server that names its models after private things will show them: the README says so."""
    if not isinstance(value, str):
        return None
    if '://' in value:
        return 'OTHER'
    name = re.split(r'[\\/]', value)[-1]
    if not re.fullmatch(r'[A-Za-z0-9_.:+\-]{1,128}', name):
        return 'OTHER'
    if re.search(r'(^|[^0-9])\d{1,3}(\.\d{1,3}){3}($|[^0-9])', name) or re.search(r':\d{1,5}$', name) or re.fullmatch(r'[0-9a-fA-F:]+', name) and name.count(':') >= 2:
        return 'OTHER'
    return name


def share_safe_reply(reply):
    """An adapter reply as a packet carries it, every field bounded by shape: ok and retry as booleans, the reply text as written,
    finish_reason as a code, the served model as a bare name, usage as numbers, the error and each attempt's error as codes, and
    addedToRequest reduced to standard generation settings (with a count of what was left out). No detail text and no other fields."""
    if not isinstance(reply, dict):
        return None
    out = {}
    for key in ('ok', 'retry'):
        if isinstance(reply.get(key), bool):
            out[key] = reply[key]
    if isinstance(reply.get('content'), str):
        out['content'] = reply['content']
    if 'finish_reason' in reply:
        fr = reply['finish_reason']
        out['finish_reason'] = fr if fr is None or (isinstance(fr, str) and CODE_TOKEN.fullmatch(fr)) else 'OTHER'
    if 'model' in reply:
        out['model'] = model_name(reply['model'])
    if 'usage' in reply:
        out['usage'] = counts(reply['usage'])
    if isinstance(reply.get('addedToRequest'), dict):
        out['addedToRequest'], left_out = generation_settings(reply['addedToRequest'])
        if left_out:
            out['addedToRequestOmitted'] = left_out
    if 'error' in reply:
        out['error'] = error_code(reply['error'])
    if isinstance(reply.get('attempts'), list):
        out['attempts'] = [dict(attempt=a['attempt'] if isinstance(a.get('attempt'), int) and not isinstance(a.get('attempt'), bool) else None,
                                ok=a['ok'] if isinstance(a.get('ok'), bool) else None, error=error_code(a.get('error')))
                           for a in reply['attempts'] if isinstance(a, dict)]
    return out


def packet_member(folder, name):
    """One kept file's bytes, or None when the comparison never wrote it. A file that is a link, has other hard links, or resolves
    outside the comparison's folder is refused: the lab never writes one, so it did not come from this comparison."""
    path = folder / name
    if not path.exists() and not path.is_symlink():
        return None
    if path.is_symlink() or path.resolve(strict=True) != folder.resolve(strict=True) / name or not path.is_file() or os.stat(path).st_nlink != 1:
        raise ValueError('This comparison holds a file the lab did not write, so it was not exported.')
    return path.read_bytes()


def compare_export(cid):
    """One Side by side comparison as a single zip: the files the lab kept for it, plus packet.json, which says what each file is and
    gathers what a reader checks first (each side's prompt version, completion status and token counts). Requests and prompt versions
    go in unchanged; replies and the result go in as share-safe copies, with diagnostics reduced to codes (error_code).
    Never included: the endpoint address, keys, provider error text, or anything outside this comparison's folder."""
    if not isinstance(cid, str) or not re.fullmatch(r'[0-9]{8}-[0-9]{6}-[0-9a-f]{6}', cid):
        raise ValueError('Invalid comparison')
    root = (DATA / 'compares').resolve()
    folder = DATA / 'compares' / cid
    if not folder.exists() and not folder.is_symlink():
        raise FileNotFoundError(2, 'No such comparison', cid)
    try:
        if folder.is_symlink() or folder.resolve(strict=True) != root / cid or not folder.is_dir():
            raise ValueError('This comparison folder is not one the lab wrote, so it was not exported.')
        raw = {n: packet_member(folder, n) for n in PACKET_MEMBERS}
        if raw['result.json'] is None:
            raise FileNotFoundError(2, 'No such comparison', cid)
        result = json.loads(raw['result.json'])
        replies = {x: json.loads(raw[f'reply-{x}.json']) if raw[f'reply-{x}.json'] is not None else None for x in 'ab'}
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as e:
        if isinstance(e, FileNotFoundError) and e.filename == cid:
            raise
        raise ValueError('This comparison could not be read for export.') from None
    if not isinstance(result, dict):
        raise ValueError('This comparison could not be read for export.')
    files = {n: d for n, d in raw.items() if d is not None and not n.startswith('reply-') and n != 'result.json'}
    safe = {x: share_safe_reply(replies[x]) for x in 'ab'}
    sides = {}
    shared_result = dict(result)
    for x in 'ab':
        side = result.get(x) if isinstance(result.get(x), dict) else {}
        named = lambda kind: f'{kind}-{x}.json' if raw[f'{kind}-{x}.json'] is not None else None
        # The side's error text can quote a provider body or a plugin's stderr: the packet keeps only its code.
        code = (safe[x] or {}).get('error') if 'error' in side else None
        shared_side = dict(side, error=code or 'ERROR') if 'error' in side else dict(side)
        shared_result[x] = shared_side
        sides[x] = dict(version=side.get('version'), prompt=side.get('prompt'), promptFile=named('prompt'), request=named('request'),
                        reply=named('reply'), completed='text' in side, error=shared_side.get('error'), seconds=side.get('seconds'),
                        words=side.get('words'), adapter={k: v for k, v in (safe[x] or {}).items() if k != 'content'} if safe[x] is not None else None)
        if safe[x] is not None:
            files[f'reply-{x}.json'] = json.dumps(safe[x], ensure_ascii=False, indent=1).encode()
    files['result.json'] = json.dumps(shared_result, ensure_ascii=False, indent=1).encode()
    # Four kinds of record: the current codeStatus (before-and-after snapshots, stable and complete); an earlier codeStatus that
    # counted only changes toward certain; a code map from an earlier version that hashed the files once after the run (code without
    # codeStatus); and no fingerprints at all.
    stored = result.get('codeStatus')
    if isinstance(stored, dict) and stored.get('schema') == CODE_STATUS_SCHEMA:
        code_status, code_note = stored, (
            'code holds fingerprints taken from disk just before the first side ran (lab_server.py: the code the lab process had '
            'loaded). codeStatus.stable says no file in scope changed during the run; codeStatus.complete says every file in scope was '
            'found and read (none unreadable, missing or omitted, and the folder scan finished); certain needs both.')
    elif isinstance(stored, dict):
        # The earlier codeStatus did not record missing files or scan errors, so completeness cannot be shown: certain is false.
        changed = stored.get('changedDuringRun') if isinstance(stored.get('changedDuringRun'), list) else None
        code_status = dict(schema='code-status/1 (normalized on export)', taken=stored.get('taken'), changedDuringRun=changed,
                           unreadable=stored.get('unreadable'), omitted=stored.get('omitted'),
                           stable=(not changed) if changed is not None else None, complete=None, certain=False)
        code_note = ('This comparison was recorded by an earlier version of the lab, which took fingerprints before and after the run but '
                     'did not check that every file was found and read. complete is unknown, so codeStatus.certain is false.')
    elif 'code' in result:
        code_status = dict(taken='Once, after both sides had run, by an earlier version of the lab.', stable=None, complete=None, certain=False)
        code_note = ('This comparison was made by an earlier version of the lab, which fingerprinted the code once after the run, so '
                     'code may not be the code either side ran. codeStatus.certain is false.')
    else:
        code_status, code_note = None, 'This comparison predates code fingerprints, so adapter, code and codeScope are not recorded.'
    packet = dict(
        schema='companion-lab-compare-packet/1', comparison=cid, at=result.get('at'),
        message=result.get('message'), model=result.get('model'), effort=result.get('effort'), plugin=result.get('plugin'),
        continued=result.get('continued'), historyTurns=result.get('history_turns'), sides=sides,
        # Recorded when the comparison ran. The adapter is named by its script, never its endpoint: base_url can name a private server.
        adapter=result.get('adapter'), code=result.get('code'), codeScope=result.get('codeScope'), codeStatus=code_status,
        missing=[n for n in PACKET_MEMBERS if raw[n] is None],
        files={n: hashlib.sha256(d).hexdigest() for n, d in sorted(files.items())},
        notes=['request-a/b.json and prompt-a/b.json are the lab\'s saved files, unchanged. request-a/b.json are exactly what the '
               'context plugin passed to the model adapter. The adapter may add fields, and the model may render the messages '
               'differently through its chat template.',
               'reply-a/b.json and result.json are share-safe copies, with each adapter field reduced to a safe form. Every error, '
               'whatever its shape, becomes one identifier-style code (no dots, colons or slashes) or ERROR; detail text is dropped. '
               'finish_reason is a code and usage is numbers only. The served model is the name the endpoint reported, cut to its last '
               'path part; a value shaped like an address (an IP address, host:port, user@host or a URL) becomes OTHER, and any other '
               'name is kept as reported, so a server that names its models after private things will show them. addedToRequest keeps '
               'only standard generation settings whose values meet each setting\'s contract (an effort level, a number, an integer or a '
               'boolean); addedToRequestOmitted counts anything else the adapter added. Fields beyond these are left out. The lab\'s own '
               'data/compares folder keeps the full originals.',
               'model and usage under each side\'s adapter are what the endpoint reported, when it reported them.',
               'missing lists the files this comparison never saved. For example, a side whose plugin failed while assembling its request '
               'has no request or reply, and comparisons made before packets existed have no prompt-a/b.json.',
               code_note,
               'The endpoint address and any key are never included, and the adapter metadata is reduced as described above. The message, '
               'prompts, requests and replies are included as written and can hold anything you or the model wrote: read the packet '
               'before you share it.'])
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as z:
        for name, data in sorted(files.items()):
            z.writestr(f'{cid}/{name}', data)
        z.writestr(f'{cid}/packet.json', json.dumps(packet, ensure_ascii=False, indent=1))
    return buffer.getvalue()


def spath(sid):
    return SESSIONS / (identity(sid) + '.json')


def read_session(sid):
    """A misplaced record must never redirect a save into another conversation."""
    session = read(spath(sid))
    if not isinstance(session, dict) or session.get('id') != sid:
        raise ValueError('The saved conversation has a conflicting identity; its file has been kept.')
    for name in ('turns', 'discarded', 'failures'):
        if not isinstance(session.get(name), list):
            raise ValueError('The saved conversation is malformed; its file has been kept.')
    return session


def session_list():
    """A broken conversation stays on disk and cannot hide the other conversations."""
    sessions, unavailable = [], []
    for path in SESSIONS.glob('*.json'):
        try:
            s = read(path)
            if s['id'] != identity(path.stem) or not isinstance(s['turns'], list):
                raise ValueError('Invalid session')
            row = {key: s[key] for key in ('id', 'created', 'model', 'effort', 'prompt')}
            if not all(isinstance(value, str) for value in row.values()):
                raise ValueError('Invalid session metadata')
            sessions.append((path.stat().st_mtime_ns, dict(row, turns=len(s['turns']))))
        except (OSError, ValueError, KeyError, TypeError):
            unavailable.append(path.stem)
    sessions.sort(key=lambda pair: pair[0], reverse=True)
    return [row for _, row in sessions], sorted(unavailable)


def create(model, effort):
    if model != SCRIPTED and model not in MODELS:
        raise ValueError('Choose an available model. If none are listed, check that the model server is running; the lab looks again shortly.')
    if effort not in EFFORTS:
        raise ValueError('Choose an available effort')
    sid = uuid.uuid4().hex
    cfg = config()
    s = dict(id=sid, created=time.strftime('%Y-%m-%d %H:%M:%S'), model=model, effort=effort, config=cfg, prompt=digest(cfg), turns=[], discarded=[], failures=[])
    if not STATEFUL:
        write(spath(sid), s)
        return s
    # A stateful plugin may make its own state for the new conversation and then fail, or the save after it may fail. Fence it like
    # every other stateful change: the record stays until both stores hold the conversation, and the page lists it if not.
    pending = DATA / 'pending' / (sid + '.json')
    write(pending, dict(schema=1, operation='/api/new', sessionBefore=None, input=dict(model=model, effort=effort), session=s))
    try:
        state = plugin('create', session=s)['state']
        if not isinstance(state, dict) or s.keys() & state.keys():
            raise ValueError('Plugin state must be an object and cannot replace core session fields.')
        s.update(state)
        write(spath(sid), s)
    except Exception as e:
        note = f'{e} The conversation was not created. Its record is kept as pending/{sid}.json for the plugin owner to check.'
        raise (ValueError if isinstance(e, ValueError) else RuntimeError)(note) from e
    pending.unlink()
    return s


def unfinished_creates():
    """Stateful creations that failed: a kept record with no saved conversation (see create)."""
    out = []
    for path in sorted((DATA / 'pending').glob('*.json')) if (DATA / 'pending').exists() else []:
        try:
            if read(path).get('operation') == '/api/new' and not spath(path.stem).exists():
                out.append(path.stem)
        except (OSError, ValueError, AttributeError):
            out.append(path.stem)
    return out


def turn(s, message, request_id=None):
    message = message.strip() if isinstance(message, str) else ''
    if not message:
        raise ValueError('Enter a message')
    rid = identity(request_id) if request_id is not None else uuid.uuid4().hex
    if (REQUESTS / s['id'] / (rid + '.json')).exists():
        raise RuntimeError('That request was already attempted. Inspect its saved output before starting another request.')
    index = len(s['turns'])
    if STATEFUL:
        plugin('checkpoint', session=s, index=index)
    started = time.monotonic()
    request = plugin('assemble', session=s, message=message, request_id=rid, budget=CONFIG.get('token_budget'))['request']
    write(REQUESTS / s['id'] / (rid + '.json'), request)
    reply = scripted_call(request) if s.get('model') == SCRIPTED else model_call(request, s['effort'])
    # Keep the returned output before invoking a stateful plugin. If its commit
    # succeeds but the session save fails, the output is still available to recover.
    write(REQUESTS / s['id'] / (rid + '-reply.json'), reply)
    if not reply.get('ok') or not isinstance(reply.get('content'), str):
        s['failures'].append(dict(at=time.strftime('%H:%M:%S'), message=message, stage='model', error=reply.get('error'), detail=reply.get('detail'), attempts=reply.get('attempts')))
        write(spath(s['id']), s)
        reason = reply.get('detail') or f"The model did not complete after {len(reply.get('attempts', []))} attempts ({reply.get('error')})."
        raise RuntimeError(f"{reason} No turn was added.")
    done = plugin('commit', session=s, request_id=rid, raw=reply['content'], message=message)
    write(REQUESTS / s['id'] / (rid + '-commit.json'), done)
    if done.get('ok') is not True:
        s['failures'].append(dict(at=time.strftime('%H:%M:%S'), message=message, stage='plugin', outcome=done.get('outcome'), reasons=done.get('reason')))
        write(spath(s['id']), s)
        raise RuntimeError(f"The context plugin refused this turn: {done.get('reason')}. Your message is back in the box.")
    text = done['text']
    reserved = {'user', 'reply', 'requestId', 'commit', 'prompt', 'parts', 'effort', 'seconds', 'words', 'attempts', 'at'}
    meta = done.get('meta', {})
    if not isinstance(text, str) or not isinstance(meta, dict) or reserved.intersection(meta):
        raise ValueError('The context plugin returned invalid text or overwrote a core turn field in meta.')
    s['turns'].append(dict(user=message, reply=text, requestId=rid, commit=done.get('commit'), prompt=s['prompt'], parts=[p['name'] for p in s['config']['parts'] if p['enabled'] and (index == 0 or p.get('when') != 'opening')], effort=s['effort'],
                           seconds=round(time.monotonic() - started, 1), words=len(text.split()), attempts=len(reply.get('attempts', [])), at=time.strftime('%Y-%m-%d %H:%M:%S'), **meta))
    write(spath(s['id']), s)
    return s


def rewind(s, index, save=True):
    """save=False when a later step of the same action saves: the file then never holds a half-finished action."""
    if type(index) != int or not 0 <= index < len(s['turns']):
        raise ValueError('Invalid turn')
    keep = uuid.uuid4().hex
    if STATEFUL:
        plugin('drop', session=s, index=index, keep=keep)
    s['discarded'].append(dict(at=time.strftime('%Y-%m-%d %H:%M:%S'), turns=s['turns'][index:], state=keep, index=index, prefix=[t['requestId'] for t in s['turns'][:index]]))
    s['turns'] = s['turns'][:index]
    if save:
        write(spath(s['id']), s)
    return s


def restorable(s, position):
    """Every check a restore makes before it changes anything. session_action runs these before opening a stateful operation
    record, so a refused restore leaves no record behind to lock the conversation."""
    if type(position) != int or not 0 <= position < len(s['discarded']):
        raise ValueError('Invalid dropped turn')
    b = s['discarded'][position]
    if not b.get('state') or (STATEFUL and not plugin('has_kept', session=s, keep=b['state'])['ok']):
        raise ValueError('That turn was dropped before restore existed, so it can only be read.')
    if [t['requestId'] for t in s['turns'][:b['index']]] != b['prefix']:
        raise ValueError('The conversation before that turn has changed, so it cannot be restored.')
    return b


def restore(s, position):
    b = restorable(s, position)
    index = b['index']
    if len(s['turns']) > index:
        s = rewind(s, index, save=False)  # What is live now becomes a dropped turn, so nothing is lost by swapping. Saved once, below.
    if STATEFUL:
        plugin('undrop', session=s, index=index, count=len(b['turns']), keep=b['state'])
    s['turns'] = s['turns'][:index] + b['turns']
    del s['discarded'][position]
    write(spath(s['id']), s)
    return s


def session_basis(session):
    """The page's view of the story and controls, without resending prompt text."""
    return dict(turns=[turn['requestId'] for turn in session['turns']],
                discarded=[branch.get('state') for branch in session['discarded']],
                prompt=session['prompt'], effort=session['effort'])


def session_action(route, session, body):
    """Keep an interrupted stateful change fenced until its plugin owner resolves it.

    A plugin and the lab write different stores. A process exit or a failed local
    save cannot prove that the plugin rolled back. Never silently run it again.
    """
    pending = DATA / 'pending' / (identity(session['id']) + '.json')
    if pending.exists():
        raise RuntimeError(f'An unfinished plugin operation needs recovery before this conversation can change. Keep and inspect {pending}.')

    if route == '/api/turn' and 'requestId' in body:
        request_id = identity(body['requestId'])
        matches = [turn for turn in session['turns'] if turn['requestId'] == request_id]
        if matches:
            if len(matches) == 1 and isinstance(body.get('message'), str) and matches[0]['user'] == body['message'].strip():
                return session  # The same completed Send is a read-back, never a second generation.
            raise ValueError('That request id belongs to a different turn')
        if any(turn['requestId'] == request_id for branch in session['discarded'] for turn in branch['turns']):
            raise ValueError('That request id belongs to a dropped turn')
        if (REQUESTS / session['id'] / (request_id + '.json')).exists():
            raise RuntimeError('That request was already attempted. Inspect its saved output before starting another request.')
    if 'basis' in body and body['basis'] != session_basis(session):
        raise ValueError('This conversation changed in another window. Reload it before trying again.')

    # Refuse ordinary input errors before opening a stateful operation record.
    if route == '/api/turn' and (not isinstance(body.get('message'), str) or not body['message'].strip()):
        raise ValueError('Enter a message')
    if route == '/api/regenerate' and not session['turns']:
        raise ValueError('Nothing to regenerate')
    if route in ('/api/rewind', '/api/restore'):
        collection = session['turns'] if route == '/api/rewind' else session['discarded']
        if type(body.get('index')) is not int or not 0 <= body['index'] < len(collection):
            raise ValueError('Invalid turn')
    if route == '/api/restore':
        restorable(session, body['index'])

    fenced = STATEFUL and route in ('/api/turn', '/api/regenerate', '/api/rewind', '/api/restore')
    if fenced:
        write(pending, dict(schema=1, operation=route, sessionBefore=session, input=body))

    if route == '/api/turn':
        result = turn(session, body['message'], body.get('requestId'))
    elif route == '/api/regenerate':
        message = session['turns'][-1]['user']
        before = json.loads(json.dumps(session))
        try:
            result = turn(rewind(session, len(session['turns']) - 1, save=False), message)
        except Exception:
            if not STATEFUL:
                # With no plugin state to reconcile, a failed regenerate keeps the reply it was replacing: live, not dropped.
                # The failure is still recorded. (A stateful plugin's failure stays fenced for its owner, as above.)
                write(spath(session['id']), dict(before, failures=session['failures']))
            raise
    elif route == '/api/rewind':
        result = rewind(session, body['index'])
    elif route == '/api/restore':
        result = restore(session, body['index'])
    else:
        if route == '/api/adopt':
            session['config'] = config()
            session['prompt'] = digest(session['config'])
        elif route == '/api/effort':
            if body['effort'] not in EFFORTS:
                raise ValueError('Unsupported effort')
            session['effort'] = body['effort']
        else:
            raise ValueError('Unknown session action')
        write(spath(session['id']), session)
        result = session

    # A failure above deliberately leaves the record, even across a server restart.
    if fenced:
        pending.unlink()
    return result


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, status, value, kind='application/json', download=None):
        data = value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header('Content-Type', kind)
        if download:
            self.send_header('Content-Disposition', f'attachment; filename="{download}"')
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.end_headers()
        self.wfile.write(data)

    def valid_host(self):
        return self.headers.get('Host') in (f'127.0.0.1:{PORT}', f'localhost:{PORT}')

    def do_GET(self):
        try:
            self.get()
        except (OSError, ValueError, TypeError, KeyError):
            self.send(500, {'error': 'The saved lab configuration could not be read. Inspect the config and data/prompts files; their bytes have been kept.'})

    def get(self):
        if not self.valid_host():
            return self.send(403, {'error': 'Invalid host'})
        if self.path == '/':
            return self.send(200, (ROOT / 'lab.html').read_bytes(), 'text/html; charset=utf-8')
        if self.path == '/plugin/display.js':
            return self.send(200, (PLUGIN_DIR / PLUGIN['display']).read_bytes() if PLUGIN.get('display') else b'', 'text/javascript; charset=utf-8')
        if self.path == '/api/state':
            discover_again()
            sessions, unavailable = session_list()
            default = MODEL.get('default_model')
            return self.send(200, dict(application='companions-lab', sessions=sessions, unavailableSessions=unavailable, unfinishedCreates=unfinished_creates(), models=MODELS, efforts=EFFORTS,
                                       defaultModel=default if default in MODELS else (MODELS[0] if MODELS else None), defaultEffort=MODEL.get('default_effort', EFFORTS[0]), config=config(), history=sorted(p.name for p in (PROMPTS / 'history').glob('*.json'))[-60:],
                                       plugin={k: PLUGIN.get(k) for k in ('id', 'roles', 'positions', 'settings', 'labels', 'parts_note', 'view_label', 'view_title', 'imports', 'exports')}, stateful=STATEFUL))
        return self.send(404, {'error': 'Not found'})

    def do_POST(self):
        held = False
        try:
            size = int(self.headers.get('Content-Length', '0'))
            if size <= 0:
                raise ValueError('Invalid request size')
            if size > REQUEST_MAX:
                raise ValueError('That request is too large.')
            # Consume a bounded body before returning a rejection. Closing with
            # unread input can reset the connection and hide the JSON error on Windows.
            self.connection.settimeout(CONFIG.get('request_read_timeout_seconds', 15))
            raw = self.rfile.read(size)
            if len(raw) != size:
                raise ValueError('Incomplete request body')
            if not self.valid_host() or self.headers.get('Origin', ORIGIN) not in (ORIGIN, f'http://localhost:{PORT}') or self.headers.get('X-Companion-Lab') != '1':
                return self.send(403, {'error': 'Use the local lab'})
            b = json.loads(raw)
            if not isinstance(b, dict):
                raise ValueError('Expected a JSON object')
            route = self.path
            if route == '/api/load':
                return self.send(200, read_session(b['id']))
            if route == '/api/prompt/version':
                if not re.fullmatch(r'[0-9a-f-]+\.json', b['name']):
                    raise ValueError('Invalid version')
                return self.send(200, {'config': read(PROMPTS / 'history' / b['name'])})
            if route == '/api/preview':
                return self.send(200, plugin('preview', config=clean(b['config'])))
            if route == '/api/view':
                if not PLUGIN.get('view_label'):
                    raise ValueError('This plugin has no state to view')
                return self.send(200, plugin('view', session=read_session(b['id'])))
            if route == '/api/export_card':
                return self.send(200, export_card(b.get('config'), b.get('name', '')))
            if route == '/api/import_card':
                return self.send(200, import_card(b.get('name', ''), b.get('data', '')))
            if route == '/api/sent':
                s = read_session(b['id'])
                index = b['index']
                if type(index) is not int or not 0 <= index < len(s['turns']):
                    raise ValueError('Invalid turn')
                t = s['turns'][index]
                if 'requestId' in b and b['requestId'] != t['requestId']:
                    return self.send(409, {'error': 'This turn changed while its request was being read.'})
                return self.send(200, read(REQUESTS / s['id'] / (t['requestId'] + '.json')))
            if route == '/api/compare/export':
                return self.send(200, compare_export(b['id']), 'application/zip', f"companion-lab-compare-{b['id']}.zip")
            held = LOCK.acquire(blocking=False)
            if not held:
                return self.send(409, {'error': 'A turn is in progress.'})
            if route == '/api/compare':
                return self.send(200, compare(b))
            if route == '/api/prompt/save':
                return self.send(200, {'config': save_config(b['config'])})
            if route == '/api/new':
                return self.send(200, create(b['model'], b['effort']))
            s = read_session(b['id'])
            if route not in ('/api/turn', '/api/regenerate', '/api/rewind', '/api/restore', '/api/adopt', '/api/effort'):
                return self.send(404, {'error': 'Not found'})
            self.send(200, session_action(route, s, b))
        except FileNotFoundError as e:
            # Name the missing item, never the folder it was looked for in.
            self.send(404, {'error': f'Not found: {Path(e.filename).name}' if e.filename else 'Not found'})
        except KeyError as e:
            self.send(400, {'error': f'Missing field: {e.args[0]}' if e.args else 'Missing field'})
        except Exception as e:
            self.send(400, {'error': str(e)[-900:]})
        finally:
            if held:
                LOCK.release()


def discover():
    global MODELS
    try:
        d = json.loads(adapter(dict(op='discover'), 60).stdout)
        # require_auth keeps a lab on the sign-in it was configured for, so it can never fall back to a paid route.
        if d.get('models') and d.get('authType', MODEL.get('require_auth')) == MODEL.get('require_auth'):
            MODELS = d['models']
    except Exception:
        pass


DISCOVERY = dict(running=False, at=0.0)


def discover_again():
    """While no model is known (a local server started after the lab, or still loading), look again in the background, at most every
    10 seconds, whenever the page asks for state. Never blocks the page."""
    if MODELS or DISCOVERY['running'] or time.monotonic() - DISCOVERY['at'] < 10:
        return
    DISCOVERY.update(running=True, at=time.monotonic())

    def run():
        try:
            discover()
        finally:
            DISCOVERY['running'] = False
    threading.Thread(target=run, daemon=True).start()


if __name__ == '__main__':
    for d in (SESSIONS, PROMPTS / 'history', REQUESTS):
        d.mkdir(parents=True, exist_ok=True)
    if not (PROMPTS / 'parts.json').exists():
        save_config(read(PLUGIN_DIR / PLUGIN['seed']))
    server = ThreadingHTTPServer(('127.0.0.1', PORT), Handler)
    threading.Thread(target=discover, daemon=True).start()
    print('Companion Lab: ' + ORIGIN, flush=True)
    server.serve_forever()
