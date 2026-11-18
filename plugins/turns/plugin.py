"""Standard turns: the whole conversation, sent as ordinary messages. Copy this file to start your own context plugin.

Protocol: one JSON object on stdin, {"op": ..., "args": ...}; one JSON object on stdout. Exit nonzero to fail.
"""
import json
import re
import sys


def enabled(config, position, opening=True):
    """The enabled, non-empty parts at one position, in the editor's order. A part marked when='opening' is sent with turn 1 only."""
    return [p for p in config['parts'] if p['enabled'] and p['text'].strip() and p.get('position', 'start') == position
            and (opening or p.get('when') != 'opening')]


def messages(config, turns, message):
    """Each part is a message with its own role. Before the conversation: the system parts first, joined into one system message, then
    the other parts in order. Just before the newest message: every part there, in order, each as its own message."""
    opening = not turns
    start = enabled(config, 'start', opening)
    system = [p['text'] for p in start if p['role'] == 'system']
    out = [dict(role='system', content='\n\n'.join(system))] if system else []
    out += [dict(role=p['role'], content=p['text']) for p in start if p['role'] != 'system']
    for t in turns:
        out += [dict(role='user', content=t['user']), dict(role='assistant', content=t['reply'])]
    out += [dict(role=p['role'], content=p['text']) for p in enabled(config, 'late', opening)]
    if message is not None:
        out.append(dict(role='user', content=message))
    return out


def assemble(session, message, **_):
    return dict(request=dict(model=session['model'], messages=messages(session['config'], session['turns'], message)))


def commit(raw, **_):
    return dict(ok=True, text=raw.strip(), meta=dict(labels=[]))


def import_card(card, **_):
    """A character card (SillyTavern V1, V2 or V3 fields) mapped onto this plugin's parts. The lab has already read the file; this only maps.
    {{char}} becomes the character's name and {{user}} 'the user'. A lorebook (character_book) is not imported, and the notes say so."""
    d = card['fields']
    name = ' '.join(str(d.get('name') or 'Character').split())[:80]

    def text(key):
        value = d.get(key)
        if not isinstance(value, str):
            return ''
        value = re.sub(r'\{\{\s*char\s*\}\}|<BOT>', name, value, flags=re.I)
        return re.sub(r'\{\{\s*user\s*\}\}|<USER>', 'the user', value, flags=re.I).strip()
    source = f"character card ({card.get('spec', 'card')}, {card.get('file', 'file')})"[:200]
    parts = []

    def add(part_name, role, position, value, enabled=True):
        if value:
            parts.append(dict(name=part_name, role=role, position=position, enabled=enabled, text=value, source=source))
    add('System prompt', 'system', 'start', text('system_prompt') or f'You are {name}, described below. Stay in character, and answer as {name} would.')
    add('Scenario', 'system', 'start', text('scenario'))
    add('Description', 'system', 'start', text('description'))
    add('Personality', 'system', 'start', text('personality'))
    add('First message', 'assistant', 'start', text('first_mes'))
    add('Example dialogue (off: switch on to try it)', 'assistant', 'start', text('mes_example'), enabled=False)
    add('Instructions after the conversation', 'system', 'late', text('post_history_instructions'))
    notes = [f'Imported {name}: {len(parts)} parts. Review them, then Save prompt to keep it.']
    book = d.get('character_book') if isinstance(d.get('character_book'), dict) else {}
    if book.get('entries'):
        notes.append(f"Not imported: the lorebook ({len(book['entries'])} entries). This plugin has no keyword memory.")
    greetings = [g for g in d.get('alternate_greetings') or [] if isinstance(g, str) and g.strip()]
    if greetings:
        notes.append(f'Not imported: {len(greetings)} alternate greetings (the first message is used).')
    note = f'imported character card: {name}'
    creator = text('creator_notes')
    if creator:
        notes.append("The card's creator notes: " + creator[:280])
    return dict(config=dict(parts=parts, note=note[:200]), notes=notes)


def export_card(config, name, **_):
    """This plugin's parts as a SillyTavern V2 card, the reverse of import_card. Parts named as the import names them go back to their
    fields; other enabled system parts before the conversation join the description, other assistant parts there become the first message
    if none is named, and late system parts become post-history instructions. The character's name becomes {{char}} and 'the user'
    becomes {{user}}. A card can't hold everything a prompt can, so every part with text that doesn't reach the card is named in the
    notes with the reason: never a silent loss, and never an implied lossless round trip."""
    name = ' '.join(str(name or '').split())[:80] or 'Character'
    parts = config.get('parts') or []
    on = [p for p in parts if p.get('enabled') and p.get('text', '').strip()]
    named = {p['name']: p for p in parts if p.get('text', '').strip()}
    used = []   # the parts whose text reached the card

    def back(p):
        used.append(p)
        text = re.sub(r'\bthe user\b', '{{user}}', p['text'].strip())
        # Not \b: a name that starts or ends with punctuation ("Dr. J.", "Unit #7") has no word boundary there.
        return re.sub(r'(?<!\w)' + re.escape(name) + r'(?!\w)', '{{char}}', text) if name != 'Character' else text

    def field(part_name):
        p = named.get(part_name)
        return back(p) if p and (p.get('enabled') or part_name.startswith('Example dialogue')) else ''
    known = {'System prompt', 'Scenario', 'Description', 'Personality', 'First message', 'Instructions after the conversation'}
    known |= {n for n in named if n.startswith('Example dialogue')}
    start_system = [p for p in on if p['role'] == 'system' and p.get('position', 'start') == 'start' and p['name'] not in known]
    start_assistant = [p for p in on if p['role'] == 'assistant' and p.get('position', 'start') == 'start' and p['name'] not in known]
    late = [p for p in on if p.get('position') == 'late' and p['role'] == 'system' and p['name'] not in known]
    description = '\n\n'.join(x for x in [field('Description')] + [back(p) for p in start_system] if x)
    first = field('First message') or (back(start_assistant[0]) if start_assistant else '')
    example = next((field(n) for n in named if n.startswith('Example dialogue')), '')
    post = '\n\n'.join(x for x in [field('Instructions after the conversation')] + [back(p) for p in late] if x)
    system_prompt, personality, scenario = field('System prompt'), field('Personality'), field('Scenario')

    def why(p):
        if named.get(p['name']) is not p:
            return 'another part has the same name'
        if not p.get('enabled'):
            return 'switched off'
        if p['role'] == 'user':
            return 'a card has no field for user parts'
        if p.get('position') == 'late':
            return 'a card has no field for late assistant parts'
        if p['name'].startswith('Example dialogue'):
            return 'a card has one example dialogue'
        if p['role'] == 'assistant':
            return 'a card has one first message'
        return 'a card has no field for it'
    missed = [p for p in parts if p.get('text', '').strip() and not any(p is u for u in used)]
    card = dict(spec='chara_card_v2', spec_version='2.0', data=dict(
        name=name, description=description, personality=personality, scenario=scenario, first_mes=first, mes_example=example,
        creator_notes='Exported from Companion Lab.', system_prompt=system_prompt, post_history_instructions=post,
        alternate_greetings=[], tags=[], creator='', character_version='', extensions={}))
    if not missed:
        return dict(card=card, notes=[f'Exported {name} as a V2 character card, with every part.'])
    notes = [f'Exported {name} as a V2 character card without {len(missed)} part{"s" if len(missed) > 1 else ""}:']
    notes += [f'Left out: {p["name"]} ({why(p)}).' for p in missed]
    return dict(card=card, notes=notes)


def preview(config, **_):
    parts = messages(config, [], None)
    return dict(text='\n\n'.join(f"===== {m['role'].upper()} =====\n{m['content']}" for m in parts) + '\n\n[Past turns and the new message follow, in order.]')


if __name__ == '__main__':
    request = json.load(sys.stdin)
    print(json.dumps(dict(assemble=assemble, commit=commit, preview=preview, import_card=import_card, export_card=export_card)[request['op']](**request['args']), ensure_ascii=False))
