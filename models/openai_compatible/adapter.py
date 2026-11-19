"""Model adapter for any OpenAI-compatible chat completions endpoint.

Protocol: {"op": "discover"} or {"op": "chat", "request": ..., "effort": ...} on stdin, plus "options" from the lab config.
Replies {"ok": true, "content": ...} or {"ok": false, "error": ...}. The API key is only ever read from the environment.
"""
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request


def call(options, path, body=None):
    base = options.get('base_url', 'https://api.openai.com/v1').rstrip('/')
    headers = {'Content-Type': 'application/json'}
    key = os.environ.get(options.get('api_key_env', 'OPENAI_API_KEY'))
    if key:
        headers['Authorization'] = 'Bearer ' + key
    request = urllib.request.Request(base + path, data=None if body is None else json.dumps(body).encode(), headers=headers)
    with urllib.request.urlopen(request, timeout=options.get('timeout_seconds', 600)) as r:
        return json.load(r)


def discover(options):
    if options.get('models'):
        return dict(ok=True, models=options['models'])
    return dict(ok=True, models=[m['id'] for m in call(options, '/models')['data']])


def added(effort):
    """What this adapter adds to the context plugin's request. The lab saves that request before the adapter sees it, and keeps this
    with the adapter's reply, so the two saved files together are what was sent to the endpoint."""
    return {'reasoning_effort': effort} if effort and effort != 'default' else {}


def chat(options, request, effort):
    body = dict(request, **added(effort))
    response = call(options, '/chat/completions', body)
    choice = response['choices'][0]
    message, finish = choice['message'], choice.get('finish_reason')
    content = message.get('content')
    # What the endpoint reported about this call, kept with every outcome: the model it says served it, and its token counts.
    served = {k: v for k, v in (('model', response.get('model')), ('usage', response.get('usage'))) if v is not None}
    # This adapter cannot execute tools. Truncated, filtered, or non-text output is
    # evidence of an attempted generation, not a completed conversational turn.
    if message.get('tool_calls') or message.get('function_call') or finish not in (None, 'stop'):
        return dict(ok=False, error='MODEL_INCOMPLETE', retry=False,
                    detail='The model stopped without a complete text reply.', finish_reason=finish, **served)
    if not isinstance(content, str) or not content.strip():
        return dict(ok=False, error='MODEL_NO_TEXT', retry=False,
                    detail='The model returned no text reply.', finish_reason=finish, **served)
    return dict(ok=True, content=content, finish_reason=finish, **served)


def missing_key(options):
    """A remote service with no key set fails at once, with the reason, rather than after three refused attempts. A local server
    (localhost, 127.0.0.1, ::1) needs no key."""
    host = (urllib.parse.urlsplit(options.get('base_url', 'https://api.openai.com/v1')).hostname or '').lower()
    name = options.get('api_key_env', 'OPENAI_API_KEY')
    if host not in ('localhost', '127.0.0.1', '::1') and not os.environ.get(name):
        return dict(ok=False, error='NO_API_KEY', retry=False, detail=f'Set the {name} environment variable before starting the lab, or point base_url at a local server.')
    return None


def main():
    x = json.load(sys.stdin)
    options = x.get('options') or {}
    try:
        reply = (discover(options) if x['op'] == 'discover' else None) or missing_key(options) or chat(options, x['request'], x.get('effort'))
    except urllib.error.HTTPError as e:
        # Only a rate limit or a server error is worth another attempt; a refused key, a bad request or an unknown model is not.
        reply = dict(ok=False, error=f'HTTP {e.code}: {e.read()[:300].decode("utf-8", "replace")}', retry=e.code == 429 or e.code >= 500)
    except Exception as e:
        reply = dict(ok=False, error=f'{type(e).__name__}: {str(e)[:300]}', retry=False,
                     detail='The model request did not return a readable reply. Its outcome is uncertain; it was not retried.')
    if x['op'] == 'chat' and reply.get('error') != 'NO_API_KEY':   # a request was sent (or may have been)
        reply['addedToRequest'] = added(x.get('effort'))
    print(json.dumps(reply, ensure_ascii=False))


if __name__ == '__main__':
    main()
