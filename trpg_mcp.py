"""Two isolated MCP servers plus the dashboard API on the Brain HTTP port."""
import hmac
import os
from contextlib import AsyncExitStack, asynccontextmanager
from mcp.server.fastmcp import FastMCP
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route
from trpg_store import Conflict, TrpgStore


class BearerGate:
    def __init__(self, app, token_env):
        self.app, self.token_env = app, token_env

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)
        token = os.environ.get(self.token_env, '').strip()
        # Misconfigured shared secrets fail closed, too.
        peers = {'TRPG_PLAYER_MCP_TOKEN', 'TRPG_DM_MCP_TOKEN', 'OMBRE_GATEWAY_TOKEN'} - {self.token_env}
        distinct = all(token != os.environ.get(key, '').strip() for key in peers)
        supplied = Request(scope).headers.get('authorization', '')
        if not token or not distinct or not hmac.compare_digest(supplied.encode(), ('Bearer ' + token).encode()):
            return await JSONResponse({'error': 'unauthorized'}, status_code=401,
                                      headers={'WWW-Authenticate': 'Bearer'})(scope, receive, send)
        await self.app(scope, receive, send)


def create_trpg_app(store):
    player = FastMCP('TRPG Player', host='0.0.0.0', json_response=True, stateless_http=True)
    dm = FastMCP('TRPG Keeper', host='0.0.0.0', json_response=True, stateless_http=True)

    def view(viewer, since_seq=0):
        return store.view_for(store.active_game(), viewer, since_seq)

    @player.tool()
    def get_table(since_seq: int = 0):
        return view('yanzhi', since_seq)

    @player.tool()
    def get_my_character():
        return view('yanzhi')['my_character']

    @player.tool()
    def get_clues():
        return view('yanzhi')['clues']

    @player.tool()
    def submit_action(text: str):
        return store.submit(store.active_game(), 'yanzhi', text)

    @player.tool()
    def roll_check(check_id: str):
        return store.roll_check(store.active_game(), check_id, 'yanzhi')

    @dm.tool()
    def get_state():
        state = view('dm')
        state.pop('log')
        return state

    @dm.tool()
    def get_log(since_seq: int = 0):
        return view('dm', since_seq)['log']

    @dm.tool()
    def search_module(query: str):
        return store.read_module(store.active_game(), query=query)

    @dm.tool()
    def read_module(id: str):
        return store.read_module(store.active_game(), item_id=id)

    @dm.tool()
    def narrate(public: str, private: dict[str, str] | None = None, gm_note: str | None = None):
        return store.narrate(store.active_game(), public, private, gm_note)

    @dm.tool()
    def write_recap(public: str | None = None, keeper: str | None = None):
        return store.write_recap(store.active_game(), public, keeper)

    @dm.tool()
    def request_check(owner: str, type: str, reason: str, skill: str | None = None,
                      difficulty: str = 'regular', bonus: int = 0, penalty: int = 0, san_loss: str | None = None):
        return store.request_check(store.active_game(), owner, type, skill, difficulty, bonus, penalty, san_loss, reason)

    @dm.tool()
    def secret_roll(type: str, reason: str, owner: str = 'yanzhi', skill: str | None = None,
                    difficulty: str = 'regular', bonus: int = 0, penalty: int = 0, san_loss: str | None = None):
        return store.request_check(store.active_game(), owner, type, skill, difficulty, bonus, penalty, san_loss, reason, secret=True)

    @dm.tool()
    def reveal_clue(clue_id: str, to: str):
        return store.reveal_clue(store.active_game(), clue_id, to)

    @dm.tool()
    def update_character(owner: str, changes: dict, reason: str):
        return store.update_character(store.active_game(), owner, changes, reason)

    @dm.tool()
    def set_scene(scene_id: str):
        return store.set_scene(store.active_game(), scene_id)

    @dm.tool()
    def create_character(owner: str, sheet: dict):
        return store.create_character(store.active_game(), owner, sheet)

    async def modules(request):
        return JSONResponse(store.list_modules() if request.method == 'GET' else store.import_module(await request.json()))

    async def pregens(request):
        return JSONResponse(store.list_pregens(request.path_params['module']))

    async def games(request):
        if request.method == 'GET':
            return JSONResponse(store.list_games())
        body = await request.json()
        return JSONResponse(store.create_game(body['module_id'], body.get('title'), body.get('characters')))

    async def table(request):
        return JSONResponse(store.view_for(request.path_params['game'], 'xiaoyang', int(request.query_params.get('since_seq', '0'))))

    async def action(request):
        body = await request.json()
        return JSONResponse(store.submit(request.path_params['game'], 'xiaoyang', body['text']))

    async def talk(request):
        body = await request.json()
        return JSONResponse(store.submit(request.path_params['game'], 'xiaoyang', body['text'], table_talk=True))

    async def roll(request):
        return JSONResponse(store.roll_check(request.path_params['game'], request.path_params['check'], 'xiaoyang'))

    async def settle(request):
        game = request.path_params['game']
        # Skip Yanzhi both before and after Xiaoyang's action; preserve CAS.
        body = await request.json()
        expected = body['expected_phase']
        if expected not in ('players', 'yanzhi'):
            raise ValueError('direct settlement requires players/yanzhi')
        return JSONResponse(store.advance(game, expected, 'dm'))

    async def phase(request):
        body = await request.json()
        return JSONResponse(store.advance(request.path_params['game'], body['expected_phase'], body['phase']))

    async def settings(request):
        changes = await request.json() if request.method == 'PATCH' else None
        return JSONResponse(store.game_settings(request.path_params['game'], changes))

    async def yanzhi_runtime(request):
        value = await request.json() if request.method == 'PUT' else None
        return JSONResponse(store.yanzhi_runtime(request.path_params['game'], value))

    async def yanzhi_view(request):
        return JSONResponse(store.yanzhi_view(request.path_params['game'], int(request.query_params.get('since_seq', '0'))))

    async def yanzhi_talk(request):
        body = await request.json()
        return JSONResponse(store.submit(request.path_params['game'], 'yanzhi', body['text'], table_talk=True))

    async def bad_request(request, exc):
        return JSONResponse({'error': str(exc)}, status_code=409 if isinstance(exc, Conflict) else 400)

    api = Starlette(routes=[
        Route('/modules', modules, methods=['GET', 'POST']),
        Route('/modules/{module}/pregens', pregens, methods=['GET']),
        Route('/games', games, methods=['GET', 'POST']),
        Route('/games/{game}/settings', settings, methods=['GET', 'PATCH']),
        Route('/games/{game}/yanzhi-runtime', yanzhi_runtime, methods=['GET', 'PUT']),
        Route('/games/{game}/yanzhi-view', yanzhi_view, methods=['GET']),
        Route('/games/{game}/yanzhi-table-talk', yanzhi_talk, methods=['POST']),
        Route('/games/{game}/table', table, methods=['GET']),
        Route('/games/{game}/action', action, methods=['POST']),
        Route('/games/{game}/table-talk', talk, methods=['POST']),
        Route('/games/{game}/checks/{check}/roll', roll, methods=['POST']),
        Route('/games/{game}/settle', settle, methods=['POST']),
        Route('/games/{game}/phase', phase, methods=['POST']),
    ], exception_handlers={ValueError: bad_request, KeyError: bad_request, TypeError: bad_request})
    player_app, dm_app = player.streamable_http_app(), dm.streamable_http_app()

    @asynccontextmanager
    async def lifespan(app):
        async with AsyncExitStack() as stack:
            await stack.enter_async_context(player.session_manager.run())
            await stack.enter_async_context(dm.session_manager.run())
            yield

    app = Starlette(routes=[
        Mount('/player', app=BearerGate(player_app, 'TRPG_PLAYER_MCP_TOKEN')),
        Mount('/dm', app=BearerGate(dm_app, 'TRPG_DM_MCP_TOKEN')),
        Mount('/api', app=BearerGate(api, 'OMBRE_GATEWAY_TOKEN')),
    ], lifespan=lifespan)
    app.state.player_mcp, app.state.dm_mcp = player, dm
    return app


def mount_trpg(app, config, profile_id):
    state_dir = os.environ.get('OMBRE_STATE_DIR') or config.get('state_dir') or os.path.join(os.path.dirname(os.path.abspath(config['buckets_dir'])), 'state')
    child = create_trpg_app(TrpgStore(os.path.join(state_dir, 'trpg.sqlite'), profile_id))
    original_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(parent):
        async with original_lifespan(parent) as original_state:
            async with child.router.lifespan_context(child):
                yield original_state

    app.router.lifespan_context = lifespan
    app.routes.insert(0, Mount('/trpg', app=child))
    return child
