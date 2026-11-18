"""Failure-boundary tests. All stores are disposable; no model or companion is used.

Run with: python -X utf8 -B -m unittest discover -s tests -p test_pressure.py
"""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
import urllib.request
import urllib.error


ROOT = Path(__file__).resolve().parents[1]


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class PressureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='public-lab-pressure-')
        self.addCleanup(self.temp.cleanup)
        self.data = Path(self.temp.name)
        self.env = patch.dict(os.environ, LAB_DATA=str(self.data),
                              LAB_CONFIG=str(ROOT / 'lab.config.json'))
        self.env.start()
        self.addCleanup(self.env.stop)
        self.lab = load_module('pressure_lab', ROOT / 'lab_server.py')
        self.lab.MODEL['retry_seconds'] = 0
        self.cfg = self.lab.read(ROOT / 'plugins/turns/seed.json')
        self.lab.save_config(self.cfg)

    def response(self, body, exit_code=0):
        return subprocess.CompletedProcess([], exit_code, json.dumps(body), '')

    def session(self):
        return self.lab.create(self.lab.MODELS[0], self.lab.EFFORTS[0])

    def test_all_prompt_parts_survive(self):
        cfg = dict(self.cfg, parts=self.cfg['parts'] * 12)
        self.assertGreater(len(cfg['parts']), 40)
        saved = self.lab.save_config(cfg)
        self.assertEqual(len(saved['parts']), len(cfg['parts']))

    def test_adapter_exit_failure_cannot_publish_success(self):
        with patch.object(self.lab, 'adapter', return_value=self.response(dict(ok=True, content='untrusted'), 9)) as send:
            result = self.lab.model_call({}, 'default')
        self.assertIs(result['ok'], False)
        self.assertIs(result['retry'], False)
        self.assertEqual(send.call_count, 1)

    def test_adapter_success_must_be_boolean(self):
        for ok in ('false', 1, [], None):
            with self.subTest(ok=ok), patch.object(self.lab, 'adapter', return_value=self.response(dict(ok=ok, content='x'))) as send:
                result = self.lab.model_call({}, 'default')
                self.assertIs(result['ok'], False)
                self.assertEqual(send.call_count, 1)

    def test_unknown_failure_is_not_retried(self):
        with patch.object(self.lab, 'adapter', return_value=self.response(dict(ok=False, error='CONNECTION_LOST'))) as send:
            self.lab.model_call({}, 'default')
        self.assertEqual(send.call_count, 1)

    def test_retry_requires_boolean_true(self):
        for retry in ('true', 1, None):
            with self.subTest(retry=retry), patch.object(self.lab, 'adapter', return_value=self.response(dict(ok=False, retry=retry))) as send:
                self.lab.model_call({}, 'default')
                self.assertEqual(send.call_count, 1)

    def test_explicit_retry_control(self):
        with patch.object(self.lab, 'adapter', side_effect=[self.response(dict(ok=False, retry=True)), self.response(dict(ok=True, content='ok'))]) as send:
            result = self.lab.model_call({}, 'default')
        self.assertTrue(result['ok'])
        self.assertEqual(send.call_count, 2)

    def test_timeout_does_not_retry(self):
        with patch.object(self.lab, 'adapter', side_effect=subprocess.TimeoutExpired('fake', 1)) as send:
            result = self.lab.model_call({}, 'default')
        self.assertEqual(send.call_count, 1)
        self.assertEqual(result['error'], 'ADAPTER_TIMEOUT')

    def test_torn_json_does_not_retry(self):
        with patch.object(self.lab, 'adapter', return_value=subprocess.CompletedProcess([], 0, '{', '')) as send:
            result = self.lab.model_call({}, 'default')
        self.assertIs(result['ok'], False)
        self.assertEqual(send.call_count, 1)

    def test_scripted_model_works_with_shipped_plugin(self):
        session = self.lab.create('scripted', self.lab.EFFORTS[0])
        with patch.object(self.lab, 'adapter', side_effect=AssertionError('real adapter forbidden')):
            self.lab.turn(session, 'A synthetic test message')
        self.assertEqual(len(session['turns']), 1)
        self.assertIn('no model was called', session['turns'][0]['reply'])

    def test_failed_replace_keeps_original_and_removes_temp(self):
        path = self.data / 'target.json'
        path.write_text('{"old": true}', encoding='utf-8')
        with patch.object(self.lab.os, 'replace', side_effect=OSError('disk fault')):
            with self.assertRaises(OSError):
                self.lab.write(path, dict(new=True))
        self.assertEqual(json.loads(path.read_text()), dict(old=True))
        self.assertEqual(list(self.data.glob('target.json.*.tmp')), [])

    def test_failed_serialization_removes_temp(self):
        path = self.data / 'target.json'
        with self.assertRaises(TypeError):
            self.lab.write(path, dict(value=object()))
        self.assertFalse(path.exists())
        self.assertEqual(list(self.data.glob('target.json.*.tmp')), [])

    def test_failed_model_keeps_conversation(self):
        session = self.session()
        with patch.object(self.lab, 'model_call', return_value=dict(ok=False, retry=False, error='fake', attempts=[])):
            with self.assertRaises(RuntimeError):
                self.lab.turn(session, 'not published')
        saved = self.lab.read(self.lab.spath(session['id']))
        self.assertEqual(saved['turns'], [])
        self.assertEqual(len(saved['failures']), 1)

    def test_repeated_completed_send_calls_no_plugin_or_model(self):
        session = self.lab.create('scripted', self.lab.EFFORTS[0])
        body = dict(message='one', requestId='a' * 32, basis=self.lab.session_basis(session))
        result = self.lab.session_action('/api/turn', session, body)
        with patch.object(self.lab, 'plugin', side_effect=AssertionError('no plugin call')):
            replay = self.lab.session_action('/api/turn', result, body)
        self.assertEqual(replay, result)
        self.assertEqual(len(replay['turns']), 1)
        with self.assertRaises(ValueError):
            self.lab.session_action('/api/turn', result, dict(body, message='different'))

    def test_stale_tab_refuses_before_any_plugin_call(self):
        session = self.lab.create('scripted', self.lab.EFFORTS[0])
        stale = self.lab.session_basis(session)
        self.lab.session_action('/api/turn', session, dict(message='one'))
        with patch.object(self.lab, 'plugin', side_effect=AssertionError('no plugin call')):
            for route, args in [('/api/turn', dict(message='two')), ('/api/regenerate', {}), ('/api/rewind', dict(index=0)), ('/api/adopt', {})]:
                with self.subTest(route=route), self.assertRaisesRegex(ValueError, 'another window'):
                    self.lab.session_action(route, session, dict(args, basis=stale))

    def test_previously_attempted_request_is_not_sent_again(self):
        session = self.lab.create('scripted', self.lab.EFFORTS[0])
        request_id = 'a' * 32
        self.lab.write(self.lab.REQUESTS / session['id'] / (request_id + '.json'), dict(model='fake'))
        with patch.object(self.lab, 'plugin', side_effect=AssertionError('no plugin call')):
            with self.assertRaisesRegex(RuntimeError, 'already attempted'):
                self.lab.session_action('/api/turn', session, dict(message='retry', requestId=request_id))

    def test_plugin_metadata_cannot_rewrite_core_fields(self):
        session = self.lab.create('scripted', self.lab.EFFORTS[0])
        actual = self.lab.plugin
        def plugin(op, **args):
            if op == 'commit':
                return dict(ok=True, text='reply', meta=dict(requestId='wrong'))
            return actual(op, **args)
        with patch.object(self.lab, 'plugin', side_effect=plugin):
            with self.assertRaises(ValueError):
                self.lab.turn(session, 'one')
        self.assertEqual(self.lab.read(self.lab.spath(session['id']))['turns'], [])

    def test_plugin_create_cannot_overwrite_an_existing_session(self):
        session = self.lab.create('scripted', self.lab.EFFORTS[0])
        before = self.lab.spath(session['id']).read_bytes()
        self.lab.STATEFUL = True
        with patch.object(self.lab, 'plugin', return_value=dict(state=dict(id=session['id']))):
            with self.assertRaises(ValueError):
                self.lab.create('scripted', self.lab.EFFORTS[0])
        self.assertEqual(self.lab.spath(session['id']).read_bytes(), before)

    def test_damaged_session_does_not_hide_healthy_sessions(self):
        session = self.session()
        bad = self.lab.SESSIONS / ('b' * 32 + '.json')
        bad.write_text('{', encoding='utf-8')
        server = self.lab.ThreadingHTTPServer(('127.0.0.1', 0), self.lab.Handler)
        self.lab.PORT = server.server_port
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with urllib.request.urlopen(f'http://127.0.0.1:{server.server_port}/api/state', timeout=5) as response:
                state = json.load(response)
            self.assertEqual([s['id'] for s in state['sessions']], [session['id']])
            self.assertEqual(state['unavailableSessions'], [bad.stem])
            self.assertEqual(bad.read_bytes(), b'{')
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


class HttpPressureTests(unittest.TestCase):
    """Exercise the actual handler on an OS-assigned loopback port."""

    def setUp(self):
        PressureTests.setUp(self)
        self.server = self.lab.ThreadingHTTPServer(('127.0.0.1', 0), self.lab.Handler)
        self.lab.PORT = self.server.server_port
        self.base = self.lab.ORIGIN = f'http://127.0.0.1:{self.server.server_port}'
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def post(self, route, body, headers=None):
        request = urllib.request.Request(self.base + route, data=json.dumps(body).encode(), headers=headers or {'X-Companion-Lab': '1', 'Origin': self.base})
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as error:
            with error:
                return error.code, json.loads(error.read())

    def test_origin_and_header_guards(self):
        for repeat in range(12):
            for headers in ({'X-Companion-Lab': '1', 'Origin': 'https://foreign.invalid'}, {'Origin': self.base}, {'X-Companion-Lab': '1', 'Host': 'foreign.invalid'}):
                with self.subTest(repeat=repeat, headers=headers):
                    status, _ = self.post('/api/new', {}, headers)
                    self.assertEqual(status, 403)
        self.assertEqual(list(self.lab.SESSIONS.glob('*.json')), [])

    def test_bad_bodies_do_not_break_later_requests(self):
        for body in (None, [], {}, {'id': '../outside'}):
            with self.subTest(body=body):
                status, _ = self.post('/api/load', body)
                self.assertEqual(status, 400)
        with urllib.request.urlopen(self.base + '/api/state', timeout=5) as response:
            self.assertEqual(response.status, 200)

    def test_errors_name_no_local_folders(self):
        status, body = self.post('/api/load', {'id': 'a' * 32})
        self.assertEqual(status, 404)
        self.assertNotIn(str(self.data.name), body['error'])
        self.assertNotIn('\\', body['error'])
        self.assertEqual(self.post('/api/new', {}), (400, {'error': 'Missing field: model'}))
        self.assertEqual(self.post('/api/import_card', []), (400, {'error': 'Expected a JSON object'}))

    def test_shapeless_prompt_returns_the_readable_error(self):
        (self.lab.PROMPTS / 'parts.json').write_text('{"parts": "corrupt"}')
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(self.base + '/api/state', timeout=5)
        with caught.exception as response:
            self.assertEqual(response.code, 500)

    def test_damaged_prompt_returns_a_readable_error_without_repairing_it(self):
        path = self.lab.PROMPTS / 'parts.json'
        path.write_bytes(b'{torn prompt')
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(self.base + '/api/state', timeout=5)
        with caught.exception as response:
            self.assertEqual(response.code, 500)
            self.assertIn('could not be read', json.load(response)['error'])
        self.assertEqual(path.read_bytes(), b'{torn prompt')

    def test_sent_rejects_negative_boolean_and_fractional_indices(self):
        session = self.lab.create('scripted', self.lab.EFFORTS[0])
        self.lab.session_action('/api/turn', session, dict(message='one'))
        for index in (-1, True, 0.5, '0', 1):
            with self.subTest(index=index):
                self.assertEqual(self.post('/api/sent', dict(id=session['id'], index=index))[0], 400)
        self.assertEqual(self.post('/api/sent', dict(id=session['id'], index=0))[0], 200)

    def test_concurrent_send_is_busy_but_reads_remain_available(self):
        session = self.lab.create(self.lab.MODELS[0], self.lab.EFFORTS[0])
        entered, release = threading.Event(), threading.Event()
        answers = []
        def model(*_):
            entered.set()
            if not release.wait(5):
                raise AssertionError('test release did not arrive')
            return dict(ok=True, content='fake reply', attempts=[])
        with patch.object(self.lab, 'model_call', side_effect=model):
            caller = threading.Thread(target=lambda: answers.append(self.post('/api/turn', dict(id=session['id'], message='first'))))
            caller.start()
            try:
                self.assertTrue(entered.wait(3))
                self.assertEqual(self.post('/api/turn', dict(id=session['id'], message='second'))[0], 409)
                self.assertEqual(self.post('/api/load', dict(id=session['id']))[1]['turns'], [])
                with urllib.request.urlopen(self.base + '/api/state', timeout=2) as response:
                    self.assertEqual(response.status, 200)
            finally:
                release.set()
                caller.join(5)
        self.assertEqual(answers[0][0], 200)
        self.assertEqual(len(answers[0][1]['turns']), 1)

    def test_pending_operation_holds_writes_but_allows_inspection(self):
        session = self.lab.create('scripted', self.lab.EFFORTS[0])
        path = self.data / 'pending' / (session['id'] + '.json')
        path.parent.mkdir()
        path.write_bytes(b'{torn operation record')
        for route, body in [('turn', dict(message='one')), ('rewind', dict(index=0)), ('adopt', {})]:
            with self.subTest(route=route):
                status, response = self.post('/api/' + route, dict(body, id=session['id']))
                self.assertEqual(status, 400)
                self.assertIn('recovery', response['error'])
        self.assertEqual(self.post('/api/load', dict(id=session['id']))[0], 200)
        self.assertEqual(path.read_bytes(), b'{torn operation record')

    def test_misplaced_session_cannot_redirect_a_write(self):
        first = self.lab.create('scripted', self.lab.EFFORTS[0])
        second = self.lab.create('scripted', self.lab.EFFORTS[0])
        first_path, second_path = self.lab.spath(first['id']), self.lab.spath(second['id'])
        second_path.write_bytes(first_path.read_bytes())
        before = first_path.read_bytes(), second_path.read_bytes()
        status, response = self.post('/api/adopt', dict(id=second['id']))
        self.assertEqual(status, 400)
        self.assertIn('conflicting identity', response['error'])
        self.assertEqual(before, (first_path.read_bytes(), second_path.read_bytes()))


class CompatibleAdapterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.adapter = load_module('pressure_adapter', ROOT / 'models/openai_compatible/adapter.py')

    def test_plain_response(self):
        with patch.object(self.adapter, 'call', return_value=dict(choices=[dict(message=dict(content='Hello'), finish_reason='stop')])):
            self.assertEqual(self.adapter.chat({}, {}, 'default')['content'], 'Hello')

    def test_reply_says_what_the_adapter_added(self):
        # Review 11-18: the lab saves the plugin's request before the adapter adds reasoning effort; the reply must say so.
        sent = []
        reply = dict(choices=[dict(message=dict(content='Hello'), finish_reason='stop')])
        for effort, added in (('high', {'reasoning_effort': 'high'}), ('default', {})):
            with self.subTest(effort=effort), patch.object(self.adapter, 'call', side_effect=lambda o, p, b=None: sent.append(b) or reply), \
                    patch('sys.stdin', io.StringIO(json.dumps(dict(op='chat', request=dict(messages=[]), effort=effort, options=dict(base_url='http://127.0.0.1:1'))))), \
                    contextlib.redirect_stdout(io.StringIO()) as out:
                self.adapter.main()
                self.assertEqual(json.loads(out.getvalue())['addedToRequest'], added)
                self.assertEqual({k: v for k, v in sent[-1].items() if k != 'messages'}, added)

    def test_no_silent_empty_or_incomplete_success(self):
        choices = [dict(message=dict(content=None, tool_calls=[dict(id='tool')]), finish_reason='tool_calls'),
                   dict(message=dict(content='partial'), finish_reason='length'),
                   dict(message=dict(content=None), finish_reason='content_filter'),
                   dict(message=dict(content=''), finish_reason='stop'),
                   dict(message=dict(content=['not plain text']), finish_reason='stop')]
        for choice in choices:
            with self.subTest(choice=choice), patch.object(self.adapter, 'call', return_value=dict(choices=[choice])):
                result = self.adapter.chat({}, {}, 'default')
                self.assertIs(result['ok'], False)
                self.assertIs(result['retry'], False)


class MemoryPluginTests(unittest.TestCase):
    """Real subprocess boundary, synthetic memory, no model generation."""

    def setUp(self):
        PressureTests.setUp(self)
        self.lab.STATEFUL = True
        self.lab.PLUGIN = dict(self.lab.PLUGIN, command=[sys.executable, '-B', str(ROOT / 'tests/fixtures/memory_plugin.py')])
        self.session_value = self.lab.create('scripted', self.lab.EFFORTS[0])

    def action(self, route, **body):
        self.session_value = self.lab.session_action('/api/' + route, self.session_value, body)
        return self.session_value

    def remembered(self):
        path = self.data / 'synthetic-memory' / self.session_value['id'] / 'state.json'
        return json.loads(path.read_text())['memory']

    def test_memory_round_trip_and_next_request(self):
        self.action('turn', message='first synthetic line')
        self.action('turn', message='second synthetic line')
        request = self.lab.read(self.lab.REQUESTS / self.session_value['id'] / (self.session_value['turns'][1]['requestId'] + '.json'))
        self.assertIn('first synthetic line', request['messages'][0]['content'])
        self.action('rewind', index=1)
        self.assertEqual(self.remembered(), ['first synthetic line'])
        self.assertEqual(len(self.session_value['turns']), 1)
        self.action('restore', index=0)
        self.assertEqual(self.remembered(), ['first synthetic line', 'second synthetic line'])
        self.assertEqual(len(self.session_value['turns']), 2)
        self.assertFalse((self.data / 'pending' / (self.session_value['id'] + '.json')).exists())

    def test_regenerate_swaps_memory_with_the_transcript(self):
        self.action('turn', message='one')
        self.action('turn', message='two')
        self.action('regenerate')
        self.assertEqual(self.remembered(), ['one', 'two'])
        self.assertEqual(len(self.session_value['discarded']), 1)
        self.action('restore', index=0)
        self.assertEqual(self.remembered(), ['one', 'two'])

    def test_commit_reply_lost_keeps_a_durable_fence(self):
        (self.data / 'cut-commit').touch()
        with self.assertRaises(RuntimeError):
            self.action('turn', message='committed without a reply')
        self.assertEqual(self.remembered(), ['committed without a reply'])
        self.assertEqual(self.lab.read(self.lab.spath(self.session_value['id']))['turns'], [])
        # Simulate a new process's only authority: the saved files, not the old Python object.
        reloaded = load_module('restarted_pressure_lab', ROOT / 'lab_server.py')
        with patch.object(reloaded, 'plugin', side_effect=AssertionError('must not call plugin')):
            with self.assertRaisesRegex(RuntimeError, 'recovery'):
                reloaded.session_action('/api/turn', reloaded.read(reloaded.spath(self.session_value['id'])), dict(message='retry'))
        self.assertTrue(list((self.lab.REQUESTS / self.session_value['id']).glob('*-reply.json')))

    def test_local_save_cut_after_commit_keeps_output_and_memory(self):
        original = self.lab.write
        def cut(path, value):
            if path == self.lab.spath(self.session_value['id']):
                raise OSError('synthetic session save failure')
            return original(path, value)
        with patch.object(self.lab, 'write', side_effect=cut):
            with self.assertRaises(OSError):
                self.action('turn', message='remembered')
        self.assertEqual(self.remembered(), ['remembered'])
        self.assertTrue(list((self.lab.REQUESTS / self.session_value['id']).glob('*-commit.json')))
        with self.assertRaisesRegex(RuntimeError, 'recovery'):
            self.action('regenerate')

    def test_drop_cut_does_not_run_again(self):
        self.action('turn', message='one')
        self.action('turn', message='two')
        (self.data / 'cut-drop').touch()
        with self.assertRaises(RuntimeError):
            self.action('rewind', index=1)
        self.assertEqual(self.remembered(), ['one'])
        pending = self.data / 'pending' / (self.session_value['id'] + '.json')
        before = pending.read_bytes()
        with patch.object(self.lab, 'plugin', side_effect=AssertionError('no plugin call')):
            with self.assertRaisesRegex(RuntimeError, 'recovery'):
                self.action('rewind', index=1)
        self.assertEqual(before, pending.read_bytes())

    def test_restore_cut_keeps_the_mismatched_transcript_fenced(self):
        self.action('turn', message='one')
        self.action('turn', message='two')
        self.action('rewind', index=1)
        (self.data / 'cut-undrop').touch()
        with self.assertRaises(RuntimeError):
            self.action('restore', index=0)
        self.assertEqual(self.remembered(), ['one', 'two'])
        self.assertEqual(len(self.lab.read(self.lab.spath(self.session_value['id']))['turns']), 1)
        with patch.object(self.lab, 'plugin', side_effect=AssertionError('no plugin call')):
            with self.assertRaisesRegex(RuntimeError, 'recovery'):
                self.action('restore', index=0)

    def test_stateful_model_failure_is_held_after_checkpoint(self):
        self.session_value['model'] = self.lab.MODELS[0]
        with patch.object(self.lab, 'model_call', return_value=dict(ok=False, retry=False, error='fake', attempts=[])):
            with self.assertRaises(RuntimeError):
                self.action('turn', message='not committed')
        self.assertEqual(self.remembered(), [])
        self.assertEqual(self.lab.read(self.lab.spath(self.session_value['id']))['turns'], [])
        with patch.object(self.lab, 'plugin', side_effect=AssertionError('no plugin call')):
            with self.assertRaisesRegex(RuntimeError, 'recovery'):
                self.action('turn', message='retry')

    def test_invalid_index_creates_no_fence(self):
        with self.assertRaises(ValueError):
            self.action('rewind', index=True)
        self.assertFalse((self.data / 'pending' / (self.session_value['id'] + '.json')).exists())

    def test_refused_restore_leaves_no_fence(self):
        # Review 11-18: restore's own checks ran after the operation record was written, so a refusal locked the conversation.
        self.action('turn', message='one')
        self.action('turn', message='two')
        self.action('rewind', index=1)
        self.action('rewind', index=0)   # the turn the first dropped branch followed is gone now
        with self.assertRaisesRegex(ValueError, 'has changed'):
            self.action('restore', index=0)
        self.assertFalse((self.data / 'pending' / (self.session_value['id'] + '.json')).exists())
        self.action('turn', message='after')
        self.assertEqual(self.remembered(), ['after'])

    def test_failed_create_keeps_a_record_and_saves_no_conversation(self):
        # Review 11-18: create ran the plugin before any record, so a failure could leave plugin state with nothing pointing at it.
        before = {p.name for p in self.lab.SESSIONS.glob('*.json')}
        (self.data / 'cut-create').touch()
        with self.assertRaisesRegex(RuntimeError, 'not created'):
            self.lab.create('scripted', self.lab.EFFORTS[0])
        self.assertEqual({p.name for p in self.lab.SESSIONS.glob('*.json')}, before)
        unfinished = self.lab.unfinished_creates()
        self.assertEqual(len(unfinished), 1)
        record = self.lab.read(self.data / 'pending' / (unfinished[0] + '.json'))
        self.assertEqual((record['operation'], record['session']['id']), ('/api/new', unfinished[0]))
        self.assertTrue((self.data / 'synthetic-memory' / unfinished[0] / 'state.json').exists())
        (self.data / 'cut-create').unlink()
        self.assertTrue(self.lab.spath(self.lab.create('scripted', self.lab.EFFORTS[0])['id']).exists())
        self.assertEqual(self.lab.unfinished_creates(), unfinished)

    def test_swap_restore_cut_leaves_the_saved_conversation_whole(self):
        # Review 11-18: the swap saved its rewind before the plugin's undrop, so a cut left a half-finished conversation on disk.
        for message in ('one', 'two', 'three'):
            self.action('turn', message=message)
        self.action('rewind', index=1)
        self.action('turn', message='four')
        (self.data / 'cut-undrop').touch()
        with self.assertRaises(RuntimeError):
            self.action('restore', index=0)
        saved = self.lab.read(self.lab.spath(self.session_value['id']))
        self.assertEqual([t['user'] for t in saved['turns']], ['one', 'four'])
        self.assertTrue((self.data / 'pending' / (self.session_value['id'] + '.json')).exists())


class ReviewFixTests(unittest.TestCase):
    """Findings from the 11-18 adversarial review, each failing on the code before its fix."""

    def setUp(self):
        PressureTests.setUp(self)

    def test_failed_regenerate_keeps_the_reply_it_was_replacing(self):
        session = self.lab.create(self.lab.MODELS[0], self.lab.EFFORTS[0])
        with patch.object(self.lab, 'model_call', return_value=dict(ok=True, content='first reply', attempts=[])):
            session = self.lab.session_action('/api/turn', session, dict(message='hello'))
        with patch.object(self.lab, 'model_call', return_value=dict(ok=False, retry=False, error='fake', attempts=[])):
            with self.assertRaises(RuntimeError):
                self.lab.session_action('/api/regenerate', session, {})
        saved = self.lab.read(self.lab.spath(session['id']))
        self.assertEqual([t['reply'] for t in saved['turns']], ['first reply'])
        self.assertEqual(saved['discarded'], [])
        self.assertEqual(len(saved['failures']), 1)

    def test_compare_with_a_missing_version_runs_no_side(self):
        saved = sorted(p.name for p in (self.lab.PROMPTS / 'history').glob('*.json'))[0]
        with patch.object(self.lab, 'model_call', side_effect=AssertionError('no model call')):
            with self.assertRaises(FileNotFoundError):
                self.lab.compare(dict(message='hi', model=self.lab.MODELS[0], effort=self.lab.EFFORTS[0], a=saved, b='20261118-000000-000000000000.json'))
        self.assertFalse((self.lab.DATA / 'compares').exists())

    def test_shapeless_saved_prompt_is_refused(self):
        for value in ({'parts': 'corrupt'}, {}, [], {'parts': ['text']}):
            with self.subTest(value=value):
                (self.lab.PROMPTS / 'parts.json').write_text(json.dumps(value))
                with self.assertRaises(ValueError):
                    self.lab.config()

    def test_saved_prompt_with_roles_another_plugin_lacks_still_loads(self):
        self.lab.PLUGIN = dict(self.lab.PLUGIN, roles=['system'])
        self.assertTrue(self.lab.config()['parts'])

    def test_export_names_every_part_it_leaves_out(self):
        # Review 11-18: an extra assistant part vanished from the export while it reported only success.
        part = lambda name, role, text, **kw: dict(name=name, role=role, enabled=kw.get('enabled', True), text=text, position=kw.get('position', 'start'))
        cfg = dict(self.cfg, parts=[part('Description', 'system', 'A reader.'), part('First message', 'assistant', 'Hello.'),
                                    part('Context', 'assistant', 'Earlier, she said yes.'), part('Aside', 'user', 'Be brief.'),
                                    part('Late aside', 'assistant', 'Later.', position='late'), part('Off', 'system', 'Unused.', enabled=False),
                                    part('Description', 'system', 'A second description.')])
        result = self.lab.export_card(cfg, 'Ada')
        notes = ' '.join(result['notes'])
        self.assertIn('without 5 parts', notes)
        for expected in ('Context (a card has one first message)', 'Aside (a card has no field for user parts)',
                         'Late aside (a card has no field for late assistant parts)', 'Off (switched off)', 'Description (another part has the same name)'):
            self.assertIn(expected, notes)
        self.assertEqual((result['card']['data']['description'], result['card']['data']['first_mes']), ('A second description.', 'Hello.'))

    def test_export_names_every_omission_however_many(self):
        # Review 11-18: only the first 18 omissions were named.
        parts = [dict(name=f'Aside {n}', role='user', enabled=True, text='Be brief.', position='start') for n in range(30)]
        notes = self.lab.export_card(dict(self.cfg, parts=parts), 'Ada')['notes']
        self.assertEqual(sum(note.startswith('Left out: Aside ') for note in notes), 30)

    def test_side_by_side_keeps_each_whole_adapter_reply(self):
        # Review 11-18: a turn kept the adapter's whole reply, side by side kept only its text.
        version = sorted(p.name for p in (self.lab.PROMPTS / 'history').glob('*.json'))[0]
        reply = dict(ok=True, content='Hello.', addedToRequest={'reasoning_effort': 'high'}, attempts=[dict(attempt=1, ok=True, error=None)])
        with patch.object(self.lab, 'model_call', return_value=reply):
            result = self.lab.compare(dict(message='hi', model=self.lab.MODELS[0], effort=self.lab.EFFORTS[0], a=version, b=version))
        for side in 'ab':
            self.assertEqual(self.lab.read(self.lab.DATA / 'compares' / result['id'] / f'reply-{side}.json'), reply)

    def test_export_with_nothing_left_out_says_so(self):
        cfg = dict(self.cfg, parts=[dict(name='Description', role='system', enabled=True, text='A reader.', position='start')])
        self.assertIn('with every part', self.lab.export_card(cfg, 'Ada')['notes'][0])

    def test_export_restores_char_for_punctuated_names(self):
        cfg = dict(self.cfg, parts=[dict(name='Description', role='system', enabled=True, text='Dr. J. works with the user. Ask Dr. J.')])
        card = self.lab.export_card(cfg, 'Dr. J.')['card']
        self.assertEqual(card['data']['description'], '{{char}} works with {{user}}. Ask {{char}}')


if __name__ == '__main__':
    unittest.main()
