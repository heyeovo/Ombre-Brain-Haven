"""Independent profile-scoped TRPG persistence and the sole visibility boundary."""
import json
import re
import sqlite3
import uuid
from contextlib import contextmanager
from pathlib import Path
import trpg_dice

PLAYERS = ('xiaoyang', 'yanzhi')
SHEET_FIELDS = {'name', 'occupation', 'characteristics', 'hp', 'hp_max', 'san', 'san_start', 'mp', 'luck', 'skills', 'background', 'notes'}
PHASES = {'players': {'yanzhi', 'dm'}, 'yanzhi': {'dm'}, 'dm': {'players', 'checks'}, 'checks': {'dm'}}


class Conflict(ValueError):
    pass


def uid():
    return uuid.uuid4().hex


def validate_module(data):
    if not isinstance(data, dict) or data.get('system') != 'coc7':
        raise ValueError('module system must be coc7')
    for key in ('title', 'public_intro', 'keeper_overview'):
        if not isinstance(data.get(key), str) or not data[key].strip():
            raise ValueError('missing ' + key)
    for collection, fields in {'scenes': ('id', 'title', 'keeper_text'), 'clues': ('id', 'title', 'text'),
                               'npcs': ('id', 'name', 'keeper_text'), 'pregens': ('name', 'occupation')}.items():
        if not isinstance(data.get(collection), list):
            raise ValueError('missing ' + collection)
        seen = set()
        for item in data[collection]:
            if not isinstance(item, dict) or any(not isinstance(item.get(k), str) or not item[k] for k in fields):
                raise ValueError('invalid ' + collection)
            if 'id' in fields:
                if item['id'] in seen:
                    raise ValueError('duplicate module id')
                seen.add(item['id'])
            if collection in ('pregens', 'npcs') and not isinstance(item.get('sheet'), dict):
                raise ValueError('invalid sheet')
            if collection == 'clues' and type(item.get('handout')) is not bool:
                raise ValueError('invalid handout')
    ids = [x['id'] for key in ('scenes', 'clues', 'npcs') for x in data[key]]
    if len(ids) != len(set(ids)):
        raise ValueError('module ids must be globally unique')


class TrpgStore:
    def __init__(self, path, profile_id='default', randbelow=None):
        self.path, self.profile_id, self.randbelow = str(path), profile_id, randbelow
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    @contextmanager
    def db(self):
        conn = sqlite3.connect(self.path, timeout=15)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute('BEGIN IMMEDIATE')
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def initialize(self):
        schemas = {
            'modules': 'id TEXT PRIMARY KEY, title TEXT, public_intro TEXT, keeper_json TEXT',
            'games': "id TEXT PRIMARY KEY, title TEXT, module_id TEXT, phase TEXT, scene_id TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP, updated_at TEXT DEFAULT CURRENT_TIMESTAMP",
            'characters': 'game_id TEXT, owner TEXT, sheet_json TEXT, PRIMARY KEY(profile_id, game_id, owner)',
            'log': 'game_id TEXT, seq INTEGER, kind TEXT, visible_to TEXT, author TEXT, text TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP, PRIMARY KEY(profile_id, game_id, seq)',
            'reveals': 'game_id TEXT, clue_id TEXT, recipient TEXT, PRIMARY KEY(profile_id, game_id, clue_id, recipient)',
            'checks': "id TEXT PRIMARY KEY, game_id TEXT, owner TEXT, type TEXT, skill TEXT, difficulty TEXT, bonus INTEGER, penalty INTEGER, san_loss TEXT, reason TEXT, status TEXT, result TEXT, secret INTEGER DEFAULT 0"}
        with self.db() as c:
            for name, schema in schemas.items():
                table = 'trpg_' + name
                c.execute(f'CREATE TABLE IF NOT EXISTS {table} (profile_id TEXT NOT NULL DEFAULT \'default\', {schema})')
                columns = {r['name'] for r in c.execute(f'PRAGMA table_info({table})')}
                if 'profile_id' not in columns:
                    # Rebuild legacy primary keys too: ADD COLUMN alone would leave
                    # character/reveal UPSERTs unable to enforce profile isolation.
                    legacy = table + '_legacy'
                    c.execute(f'ALTER TABLE {table} RENAME TO {legacy}')
                    c.execute(f"CREATE TABLE {table} (profile_id TEXT NOT NULL DEFAULT 'default', {schema})")
                    new_columns = {r['name'] for r in c.execute(f'PRAGMA table_info({table})')}
                    shared = sorted(columns & new_columns)
                    quoted = ','.join('"' + name + '"' for name in shared)
                    c.execute(f'INSERT INTO {table} ({quoted}) SELECT {quoted} FROM {legacy}')
                    c.execute(f'DROP TABLE {legacy}')
                    columns = new_columns
                if name in ('games', 'modules'):
                    field = 'ended_at' if name == 'games' else 'deleted_at'
                    if field not in columns:
                        c.execute(f'ALTER TABLE {table} ADD COLUMN {field} TEXT')
                if name == 'games':
                    for field in ('settings_json', 'runtime_json'):
                        if field not in columns:
                            c.execute(f"ALTER TABLE {table} ADD COLUMN {field} TEXT NOT NULL DEFAULT '{{}}'")
                if name == 'checks' and 'secret' not in columns:
                    c.execute(f'ALTER TABLE {table} ADD COLUMN secret INTEGER NOT NULL DEFAULT 0')

    def _game(self, c, game, *, writable=False):
        row = c.execute('SELECT * FROM trpg_games WHERE profile_id=? AND id=?', (self.profile_id, game)).fetchone()
        if not row:
            raise ValueError('game not found')
        if writable and row['ended_at'] is not None:
            raise Conflict('game ended')
        return dict(row)

    def _module(self, c, module, *, available=False):
        row = c.execute('SELECT keeper_json FROM trpg_modules WHERE profile_id=? AND id=?' + (' AND deleted_at IS NULL' if available else ''), (self.profile_id, module)).fetchone()
        if not row:
            raise ValueError('module not found')
        return json.loads(row[0])

    def _log(self, c, game, kind, visible, author, text):
        seq = c.execute('SELECT COALESCE(MAX(seq),0)+1 FROM trpg_log WHERE profile_id=? AND game_id=?', (self.profile_id, game)).fetchone()[0]
        c.execute('INSERT INTO trpg_log(profile_id,game_id,seq,kind,visible_to,author,text) VALUES(?,?,?,?,?,?,?)',
                  (self.profile_id, game, seq, kind, visible, author, text))
        c.execute('UPDATE trpg_games SET updated_at=CURRENT_TIMESTAMP WHERE profile_id=? AND id=?', (self.profile_id, game))

    def import_module(self, data):
        validate_module(data)
        module = data.get('id', uid())
        if not isinstance(module, str) or not re.fullmatch(r'[A-Za-z0-9_-]+', module):
            raise ValueError('invalid module id')
        with self.db() as c:
            existing = c.execute('SELECT profile_id,deleted_at FROM trpg_modules WHERE id=?', (module,)).fetchone()
            if existing and (existing['profile_id'] != self.profile_id or existing['deleted_at'] is None):
                raise Conflict('module already exists')
            c.execute('INSERT INTO trpg_modules(profile_id,id,title,public_intro,keeper_json) VALUES(?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET title=excluded.title,public_intro=excluded.public_intro,keeper_json=excluded.keeper_json,deleted_at=NULL', (self.profile_id, module, data['title'], data['public_intro'], json.dumps(data)))
        return {'title': data['title'], **{key + '_count': len(data[key]) for key in ('scenes', 'clues', 'npcs')}}

    def end_game(self, game):
        with self.db() as c:
            g = self._game(c, game)
            if g['ended_at'] is None:
                runtime = json.loads(g['runtime_json'])
                runtime.update(running_since=None, last_error=None)
                c.execute('UPDATE trpg_games SET ended_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP,runtime_json=? WHERE profile_id=? AND id=?', (json.dumps(runtime), self.profile_id, game))
            return {'ended_at': self._game(c, game)['ended_at']}

    def delete_module(self, module_id):
        with self.db() as c:
            self._module(c, module_id)
            if c.execute('SELECT 1 FROM trpg_games WHERE profile_id=? AND module_id=? AND ended_at IS NULL', (self.profile_id, module_id)).fetchone():
                raise Conflict('active game uses module')
            c.execute('UPDATE trpg_modules SET deleted_at=COALESCE(deleted_at,CURRENT_TIMESTAMP) WHERE profile_id=? AND id=?', (self.profile_id, module_id))
        return {'ok': True}

    def list_modules(self):
        with self.db() as c:
            return [dict(r) for r in c.execute('SELECT id,title FROM trpg_modules WHERE profile_id=? AND deleted_at IS NULL', (self.profile_id,))]

    def list_pregens(self, module):
        with self.db() as c:
            return [dict(index=i, name=pregen['name'], occupation=pregen['occupation'],
                         sheet={key: value for key, value in pregen['sheet'].items() if key in SHEET_FIELDS})
                    for i, pregen in enumerate(self._module(c, module, available=True)['pregens'])]

    def write_recap(self, game, public=None, keeper=None):
        for text in (public, keeper):
            if text is not None and not isinstance(text, str):
                raise ValueError('invalid recap text')
        if not any(text and text.strip() for text in (public, keeper)):
            raise ValueError('empty recap')
        with self.db() as c:
            self._game(c, game, writable=True)
            for recipient, text in (('all', public), ('dm', keeper)):
                if text and text.strip():
                    self._log(c, game, 'recap', recipient, 'dm', text)
        return {'ok': True}

    def latest_recap(self, game, viewer):
        logs = self.view_for(game, viewer)['log']
        result = {}
        for log in logs:
            if log['kind'] == 'recap':
                result['public' if log['visible_to'] == 'all' else 'keeper'] = log['text']
        return result

    def game_settings(self, game, changes=None):
        defaults = {'yanzhi_model': 'claude-opus-4-6', 'persona_id': '', 'context_pinned': True, 'context_review_days': 5, 'context_main_rounds': 10}
        if changes is not None:
            if not isinstance(changes, dict) or set(changes) - set(defaults):
                raise ValueError('invalid settings')
            if 'yanzhi_model' in changes and (not isinstance(changes['yanzhi_model'], str) or not re.fullmatch(r'claude-(opus|sonnet)-[a-z0-9-]+', changes['yanzhi_model'])):
                raise ValueError('invalid yanzhi_model')
            if 'persona_id' in changes and not isinstance(changes['persona_id'], str):
                raise ValueError('invalid persona_id')
            if 'context_pinned' in changes and type(changes['context_pinned']) is not bool:
                raise ValueError('invalid context_pinned')
            for key, maximum in (('context_review_days', 7), ('context_main_rounds', 30)):
                if key in changes and (type(changes[key]) is not int or not 0 <= changes[key] <= maximum):
                    raise ValueError('invalid ' + key)
        with self.db() as c:
            settings = {**defaults, **json.loads(self._game(c, game, writable=changes is not None)['settings_json'])}
            if changes is not None:
                reset = any(key in changes and changes[key] != settings[key] for key in ('persona_id', 'context_pinned', 'context_review_days', 'context_main_rounds'))
                if reset:
                    runtime = json.loads(self._game(c, game)['runtime_json'])
                    runtime.update(session_id=None, session_tokens=0)
                    c.execute('UPDATE trpg_games SET runtime_json=? WHERE profile_id=? AND id=?', (json.dumps(runtime), self.profile_id, game))
                settings.update(changes)
                c.execute('UPDATE trpg_games SET settings_json=? WHERE profile_id=? AND id=?', (json.dumps(settings), self.profile_id, game))
            return settings

    def yanzhi_runtime(self, game, value=None):
        defaults = dict(session_id=None, session_tokens=0, last_seen_seq=0, last_error=None, running_since=None)
        expected_settings = None
        if isinstance(value, dict) and 'expected_settings' in value:
            value = dict(value)
            expected_settings = value.pop('expected_settings')
            if not isinstance(expected_settings, dict):
                raise ValueError('invalid expected_settings')
        if value is not None:
            if not isinstance(value, dict) or set(value) - set(defaults):
                raise ValueError('invalid runtime')
            for key in ('session_tokens', 'last_seen_seq'):
                if key in value and (type(value[key]) is not int or value[key] < 0):
                    raise ValueError('invalid ' + key)
            for key in ('session_id', 'last_error', 'running_since'):
                if key in value and value[key] is not None and not isinstance(value[key], str):
                    raise ValueError('invalid ' + key)
        with self.db() as c:
            runtime = {**defaults, **json.loads(self._game(c, game, writable=value is not None)['runtime_json'])}
            if value is not None:
                if expected_settings is not None:
                    # Compare and save under the same BEGIN IMMEDIATE lock as settings reset.
                    settings = {**{'persona_id': '', 'context_pinned': True, 'context_review_days': 5, 'context_main_rounds': 10}, **json.loads(self._game(c, game)['settings_json'])}
                    if any(settings[key] != expected_settings.get(key, default) for key, default in (('persona_id', ''), ('context_pinned', True), ('context_review_days', 5), ('context_main_rounds', 10))):
                        value = {key: item for key, item in value.items() if key not in ('session_id', 'session_tokens')}
                runtime.update(value)
                c.execute('UPDATE trpg_games SET runtime_json=? WHERE profile_id=? AND id=?', (json.dumps(runtime), self.profile_id, game))
            return runtime

    def yanzhi_view(self, game, since_seq=0):
        return {**self.view_for(game, 'yanzhi', since_seq), 'latest_recap': self.latest_recap(game, 'yanzhi')}

    def list_games(self):
        with self.db() as c:
            return [dict(r) for r in c.execute('SELECT id,title,phase,module_id,ended_at,created_at,updated_at FROM trpg_games WHERE profile_id=?', (self.profile_id,))]

    def create_game(self, module_id, title=None, characters=None):
        if title is not None and (not isinstance(title, str) or not title.strip()):
            raise ValueError('invalid game title')
        if characters is not None and (not isinstance(characters, dict) or set(characters) - set(PLAYERS)):
            raise ValueError('invalid character owners')
        game = uid()
        with self.db() as c:
            module = self._module(c, module_id, available=True)
            if c.execute('SELECT 1 FROM trpg_games WHERE profile_id=? AND ended_at IS NULL', (self.profile_id,)).fetchone():
                raise Conflict('one active game per profile')
            c.execute('INSERT INTO trpg_games(profile_id,id,title,module_id,phase,scene_id) VALUES(?,?,?,?,?,?)',
                      (self.profile_id, game, title or module['title'], module_id, 'players', module['scenes'][0]['id'] if module['scenes'] else None))
            for i, owner in enumerate(PLAYERS):
                choice = (characters or {}).get(owner, i)
                if type(choice) is int:
                    if choice < 0:
                        raise ValueError('pregen not found')
                    try:
                        pregen = module['pregens'][choice]
                    except IndexError:
                        raise ValueError('pregen not found') from None
                    sheet = {**pregen['sheet'], 'name': pregen['name'], 'occupation': pregen['occupation']}
                elif isinstance(choice, dict):
                    sheet = choice
                else:
                    raise ValueError('invalid character selection')
                self._character(c, game, owner, sheet)
            self._log(c, game, 'narration', 'all', 'dm', module['public_intro'])
        return {'id': game, 'title': title or module['title'], 'phase': 'players'}

    def active_game(self):
        games = [game for game in self.list_games() if game['ended_at'] is None]
        if len(games) != 1:
            raise ValueError('exactly one game required')
        return games[0]['id']

    def view_for(self, game, viewer, since_seq=0):
        if viewer not in (*PLAYERS, 'dm') or type(since_seq) is not int or since_seq < 0:
            raise ValueError('invalid viewer/cursor')
        with self.db() as c:
            g = self._game(c, game)
            module = self._module(c, g['module_id'])
            scene = next((s for s in module['scenes'] if s['id'] == g['scene_id']), None)
            chars = {r['owner']: json.loads(r['sheet_json']) for r in c.execute('SELECT owner,sheet_json FROM trpg_characters WHERE profile_id=? AND game_id=?', (self.profile_id, game))}
            args = [self.profile_id, game, since_seq]
            sql = 'SELECT seq,kind,visible_to,author,text,created_at FROM trpg_log WHERE profile_id=? AND game_id=? AND seq>?'
            if viewer != 'dm':
                sql += " AND kind!='gm_note' AND visible_to IN ('all',?)"
                args.append(viewer)
            logs = [dict(r) for r in c.execute(sql + ' ORDER BY seq', args)]
            reveal_sql = 'SELECT clue_id,recipient FROM trpg_reveals WHERE profile_id=? AND game_id=?'
            reveal_args = [self.profile_id, game]
            if viewer != 'dm':
                reveal_sql += " AND recipient IN ('all',?)"
                reveal_args.append(viewer)
            reveals = [dict(r) for r in c.execute(reveal_sql, reveal_args)]
            ids = {r['clue_id'] for r in reveals}
            clues = [{k: clue[k] for k in ('id', 'title', 'text', 'handout')} for clue in module['clues'] if clue['id'] in ids]
            check_sql = "SELECT id,owner,type,skill,difficulty,bonus,penalty,san_loss,reason,status,result FROM trpg_checks WHERE profile_id=? AND game_id=? AND status='pending'"
            check_args = [self.profile_id, game]
            if viewer != 'dm':
                check_sql += ' AND owner=? AND secret=0'
                check_args.append(viewer)
            checks = [dict(r) for r in c.execute(check_sql, check_args)]
            result = dict(id=game, title=g['title'], phase=g['phase'], ended_at=g['ended_at'], module_id=g['module_id'], scene=({'id': scene['id'], 'title': scene['title']} if scene else None), log=logs, clues=clues, checks=checks)
            if viewer == 'dm':
                result.update(characters=chars, reveals=reveals)
            else:
                result.update(my_character=chars.get(viewer), companions=[{'owner': o, 'name': s.get('name', ''), 'occupation': s.get('occupation', '')} for o, s in chars.items() if o in PLAYERS and o != viewer])
            return result

    def advance(self, game, expected_phase, phase):
        with self.db() as c:
            g = self._game(c, game, writable=True)
            if g['phase'] != expected_phase:
                raise Conflict('phase CAS conflict')
            if phase not in PHASES.get(expected_phase, set()):
                raise ValueError('invalid transition')
            pending = c.execute("SELECT 1 FROM trpg_checks WHERE profile_id=? AND game_id=? AND status='pending' AND secret=0", (self.profile_id, game)).fetchone()
            if (phase == 'checks') != bool(pending) and (phase == 'checks' or expected_phase in ('checks', 'dm')):
                raise Conflict('pending checks do not match transition')
            c.execute('UPDATE trpg_games SET phase=?,updated_at=CURRENT_TIMESTAMP WHERE profile_id=? AND id=?', (phase, self.profile_id, game))
        return {'phase': phase}

    def submit(self, game, owner, text, table_talk=False):
        if owner not in PLAYERS or not isinstance(text, str) or not text.strip():
            raise ValueError('invalid submission')
        with self.db() as c:
            g = self._game(c, game, writable=True)
            if not table_talk and g['phase'] != {'xiaoyang': 'players', 'yanzhi': 'yanzhi'}[owner]:
                raise Conflict('not your turn')
            self._log(c, game, 'table_talk' if table_talk else 'action', 'all', owner, text)
            if not table_talk:
                c.execute('UPDATE trpg_games SET phase=? WHERE profile_id=? AND id=?', ('yanzhi' if owner == 'xiaoyang' else 'dm', self.profile_id, game))
        return {'ok': True}

    def _dm_turn(self, c, game):
        if self._game(c, game, writable=True)['phase'] != 'dm':
            raise Conflict('not DM turn')

    def narrate(self, game, public, private=None, gm_note=None):
        if private and (not isinstance(private, dict) or set(private) - set(PLAYERS)):
            raise ValueError('invalid private recipients')
        with self.db() as c:
            self._dm_turn(c, game)
            for kind, recipient, text in [('narration', 'all', public), *[('private', o, t) for o, t in (private or {}).items()], ('gm_note', 'dm', gm_note)]:
                if text:
                    if not isinstance(text, str):
                        raise ValueError('invalid text')
                    self._log(c, game, kind, recipient, 'dm', text)
        return {'ok': True}

    def _character(self, c, game, owner, sheet):
        if owner not in PLAYERS and not owner.startswith('npc:'):
            raise ValueError('invalid owner')
        if not isinstance(sheet, dict):
            raise ValueError('invalid sheet')
        sheet = {k: v for k, v in sheet.items() if k in SHEET_FIELDS}
        for key in ('name', 'occupation', 'background', 'notes'):
            if key in sheet and not isinstance(sheet[key], str):
                raise ValueError('invalid ' + key)
        for key in ('hp', 'hp_max', 'san', 'san_start', 'mp', 'luck'):
            if key in sheet and (type(sheet[key]) is not int or not 0 <= sheet[key] <= 100):
                raise ValueError('invalid ' + key)
        for key in ('characteristics', 'skills'):
            if key in sheet and (not isinstance(sheet[key], dict) or any(type(v) is not int or not 0 <= v <= 100 for v in sheet[key].values())):
                raise ValueError('invalid ' + key)
        c.execute('INSERT INTO trpg_characters VALUES(?,?,?,?) ON CONFLICT(profile_id,game_id,owner) DO UPDATE SET sheet_json=excluded.sheet_json', (self.profile_id, game, owner, json.dumps(sheet)))

    def create_character(self, game, owner, sheet):
        with self.db() as c:
            self._dm_turn(c, game)
            if isinstance(sheet, dict) and 'pregen' in sheet:
                index = sheet['pregen']
                module = self._module(c, self._game(c, game)['module_id'])
                if type(index) is not int or not 0 <= index < len(module['pregens']):
                    raise ValueError('pregen not found')
                pregen = module['pregens'][index]
                sheet = {**pregen['sheet'], 'name': pregen['name'], 'occupation': pregen['occupation']}
            self._character(c, game, owner, sheet)
        return {'ok': True}

    def update_character(self, game, owner, changes, reason):
        if not isinstance(changes, dict) or set(changes) - {'hp', 'san', 'mp', 'luck', 'notes'}:
            raise ValueError('invalid character changes')
        with self.db() as c:
            self._dm_turn(c, game)
            sheet = self._sheet(c, game, owner)
            sheet.update(changes)
            self._character(c, game, owner, sheet)
            self._log(c, game, 'private', owner if owner in PLAYERS else 'dm', 'dm', json.dumps({'changes': changes, 'reason': reason}))
        return {'ok': True}

    def _sheet(self, c, game, owner):
        row = c.execute('SELECT sheet_json FROM trpg_characters WHERE profile_id=? AND game_id=? AND owner=?', (self.profile_id, game, owner)).fetchone()
        if not row:
            raise ValueError('character not found')
        return json.loads(row[0])

    def read_module(self, game, item_id=None, query=None):
        """DM-only service method: never exposed by either player adapter."""
        with self.db() as c:
            module = self._module(c, self._game(c, game)['module_id'])
            items = [dict(id='overview', text=module['keeper_overview']), *module['scenes'], *module['clues'], *module['npcs'],
                     *[dict(id=f'pregen:{i}', **p) for i, p in enumerate(module['pregens'])]]
            if query is not None:
                return [item for item in items if query.casefold() in json.dumps(item, ensure_ascii=False).casefold()]
            item = next((item for item in items if item['id'] == item_id), None)
            if item is None:
                raise ValueError('module item not found')
            return item

    def set_scene(self, game, scene_id):
        with self.db() as c:
            self._dm_turn(c, game)
            module = self._module(c, self._game(c, game)['module_id'])
            if scene_id not in {s['id'] for s in module['scenes']}:
                raise ValueError('scene not found')
            c.execute('UPDATE trpg_games SET scene_id=? WHERE profile_id=? AND id=?', (scene_id, self.profile_id, game))
        return {'ok': True}

    def reveal_clue(self, game, clue_id, to):
        if to not in ('all', *PLAYERS):
            raise ValueError('invalid recipient')
        with self.db() as c:
            self._dm_turn(c, game)
            module = self._module(c, self._game(c, game)['module_id'])
            if clue_id not in {clue['id'] for clue in module['clues']}:
                raise ValueError('clue not found')
            c.execute('INSERT OR IGNORE INTO trpg_reveals VALUES(?,?,?,?)', (self.profile_id, game, clue_id, to))
        return {'ok': True}

    def request_check(self, game, owner, type, skill=None, difficulty='regular', bonus=0, penalty=0, san_loss=None, reason='', secret=False):
        if not secret and owner not in PLAYERS:
            raise ValueError('public checks require a player owner')
        if type not in ('skill', 'characteristic', 'luck', 'san'):
            raise ValueError('invalid check type')
        check_id = uid()
        with self.db() as c:
            phase = self._game(c, game, writable=True)['phase']
            if secret and phase != 'dm':
                raise Conflict('not DM turn')
            if phase not in ('dm', 'checks'):
                raise Conflict('not DM/checks phase')
            sheet = self._sheet(c, game, owner)
            target = self._target(sheet, type, skill)
            trpg_dice.roll(target, difficulty, bonus, penalty, randbelow=lambda n: 0)
            if type == 'san':
                if not isinstance(san_loss, str) or len(san_loss.split('/')) != 2:
                    raise ValueError('SAN loss requires success/failure')
                for expression in san_loss.split('/'):
                    trpg_dice.loss(expression, lambda n: 0)
            c.execute('INSERT INTO trpg_checks VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)', (self.profile_id, check_id, game, owner, type, skill, difficulty, bonus, penalty, san_loss, reason, 'pending', None, int(secret)))
            if secret:
                return self._roll(c, game, check_id, owner, True)
            c.execute("UPDATE trpg_games SET phase='checks' WHERE profile_id=? AND id=?", (self.profile_id, game))
        return {'id': check_id, 'phase': 'checks'}

    @staticmethod
    def _target(sheet, type, skill):
        try:
            return sheet[{'luck': 'luck', 'san': 'san'}[type]] if type in ('luck', 'san') else sheet['skills' if type == 'skill' else 'characteristics'][skill]
        except KeyError:
            raise ValueError('check target missing') from None

    def roll_check(self, game, check_id, owner):
        with self.db() as c:
            if self._game(c, game, writable=True)['phase'] != 'checks':
                raise Conflict('not checks phase')
            return self._roll(c, game, check_id, owner, False)

    def _roll(self, c, game, check_id, owner, secret):
        row = c.execute('SELECT * FROM trpg_checks WHERE profile_id=? AND game_id=? AND id=? AND owner=? AND secret=?', (self.profile_id, game, check_id, owner, int(secret))).fetchone()
        if not row:
            raise ValueError('check not found')
        if row['status'] != 'pending':
            raise Conflict('check already rolled')
        sheet = self._sheet(c, game, owner)
        kwargs = {'randbelow': self.randbelow} if self.randbelow else {}
        result = trpg_dice.roll(self._target(sheet, row['type'], row['skill']), row['difficulty'], row['bonus'], row['penalty'], **kwargs)
        if row['type'] == 'san':
            amount = trpg_dice.loss(row['san_loss'].split('/')[0 if result['success'] else 1], **kwargs)
            before = sheet['san']
            sheet['san'] = max(0, before - amount)
            self._character(c, game, owner, sheet)
            result.update(san_loss=amount, san_before=before, san_after=sheet['san'])
        c.execute("UPDATE trpg_checks SET status='rolled',result=? WHERE profile_id=? AND id=?", (json.dumps(result), self.profile_id, check_id))
        self._log(c, game, 'roll', 'dm' if secret else 'all', owner, json.dumps({'check_id': check_id, 'type': row['type'], 'skill': row['skill'], 'reason': row['reason'], **result}))
        if not secret and not c.execute("SELECT 1 FROM trpg_checks WHERE profile_id=? AND game_id=? AND status='pending' AND secret=0", (self.profile_id, game)).fetchone():
            c.execute("UPDATE trpg_games SET phase='dm' WHERE profile_id=? AND id=?", (self.profile_id, game))
        return result
