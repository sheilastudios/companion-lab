"""Synthetic stateful fixture using only PLUGINS.md's public protocol.

It remembers every test line without retrieval or ranking. It is not a companion
prompt or an example of the private memory system.
"""
import json
from pathlib import Path
import sys


def main():
    request = json.load(sys.stdin)
    op, args = request['op'], request['args']
    root = Path(args['data']) / 'synthetic-memory' / args['session']['id']
    root.mkdir(parents=True, exist_ok=True)
    path = root / 'state.json'
    state = json.loads(path.read_text()) if path.exists() else dict(memory=[], checkpoints={})
    if op == 'create':
        path.write_text(json.dumps(state))
        if (Path(args['data']) / 'cut-create').exists():
            raise RuntimeError('Synthetic cut after the plugin state write')
        return dict(state={})
    if op == 'assemble':
        return dict(request=dict(model='fake', messages=[dict(role='system', content='Synthetic remembered lines: ' + json.dumps(state['memory'])),
                                                       dict(role='user', content=args['message'])]))
    if op == 'has_kept':
        return dict(ok=(root / (args['keep'] + '.json')).exists())
    if op == 'checkpoint':
        state['checkpoints'][str(args['index'])] = list(state['memory'])
    elif op == 'commit':
        state['memory'].append(args['message'])
    elif op == 'drop':
        (root / (args['keep'] + '.json')).write_text(json.dumps(state))
        state['memory'] = state['checkpoints'][str(args['index'])]
    elif op == 'undrop':
        state = json.loads((root / (args['keep'] + '.json')).read_text())
    else:
        raise ValueError('Unsupported fixture operation')
    path.write_text(json.dumps(state))
    if (Path(args['data']) / ('cut-' + op)).exists():
        raise RuntimeError('Synthetic cut after the plugin state write')
    return dict(ok=True, text=args.get('raw', ''), meta=dict(labels=[]))


if __name__ == '__main__':
    print(json.dumps(main()))
