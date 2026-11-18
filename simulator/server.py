"""Simulator: a fake user that drives the real Scout chat page.

    python server.py            ->  http://127.0.0.1:8100

/            the test-case dashboard (5 happy-flow cases, 5 ambiguity cases), with Run all per group
/runs        every run in order, with totals; each row opens its saved chat (/chat/<id>)
/timeline/<id> a saved chat as who-said-what steps, turn by turn
/run/<id>    the chat page, an exact copy of the app's UI (ui/), with sim.js typing as the fake user
/api/chat    forwarded to the app under test (APP_URL, default http://127.0.0.1:8000)
/api/chats   the simulator's own history, so test chats never mix into the app's history
"""
import hashlib
import json
import os
import queue
import re
import sys
import threading
import time
import uuid
from pathlib import Path

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

import store

HERE = Path(__file__).resolve().parent
APP_URL = os.getenv('APP_URL', 'http://127.0.0.1:8000').rstrip('/')
APP_EMAIL, APP_PASSWORD = os.getenv('APP_EMAIL', ''), os.getenv('APP_PASSWORD', '')  # the account the simulator signs in as (start.ps1 takes the first SEED_USERS pair from the app's .env)
ID = re.compile(r'^[A-Za-z0-9_-]{8,64}$')
CASE_ID = re.compile(r'^[a-z]+-\d+$')
STATIC = {'/app.js': ('ui/app.js', 'text/javascript'), '/style.css': ('ui/style.css', 'text/css'), '/sim.js': ('sim.js', 'text/javascript'),
          '/dash.js': ('dash.js', 'text/javascript'), '/dash.css': ('dash.css', 'text/css'),
          '/runs.js': ('runs.js', 'text/javascript'), '/timeline.js': ('timeline.js', 'text/javascript'), '/timeline.css': ('timeline.css', 'text/css'), '/replay.js': ('replay.js', 'text/javascript'),
          '/evals.js': ('evals.js', 'text/javascript'), '/evals.css': ('evals.css', 'text/css')}

app = FastAPI(title='Scout simulator', docs_url=None, redoc_url=None)


def cases() -> list[dict]:
    return json.loads((HERE / 'cases.json').read_text('utf-8'))


def sse(event: dict) -> bytes:
    return f'data: {json.dumps(event)}\n\n'.encode()


_session = {'token': ''}


async def app_headers(extra: dict | None = None) -> dict:
    """The app only answers signed-in accounts: sign in as APP_EMAIL once and send the session token with every call to it."""
    if not _session['token'] and APP_EMAIL:
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                reply = await client.post(APP_URL + '/api/auth/login', json={'email': APP_EMAIL, 'password': APP_PASSWORD})
            if reply.status_code == 200:
                _session['token'] = reply.json()['token']
        except (httpx.HTTPError, ValueError, KeyError):
            pass
    return {**(extra or {}), **({'Authorization': 'Bearer ' + _session['token']} if _session['token'] else {})}


@app.middleware('http')
async def no_store(request: Request, call_next):
    response = await call_next(request)
    response.headers['Cache-Control'] = 'no-store'
    return response


@app.get('/api/cases')
def api_cases():
    results = store.load_results()
    return [{**case, 'last': results.get(case['id'])} for case in cases()]


@app.get('/api/app-status')
async def app_status():
    try:
        async with httpx.AsyncClient(timeout=4) as client:
            health = (await client.get(APP_URL + '/api/health')).json()
        return {'online': True, 'url': APP_URL, 'model': health.get('model')}
    except (httpx.HTTPError, ValueError):
        return {'online': False, 'url': APP_URL}


@app.post('/api/results/{case_id}')
async def api_result(case_id: str, request: Request):
    case = next((c for c in cases() if c['id'] == case_id), None) if CASE_ID.match(case_id) else None
    if case is None:
        return JSONResponse({'error': 'Unknown case.'}, status_code=404)
    body = await request.json()
    store.save_run({'run_id': uuid.uuid4().hex[:12], 'case_id': case_id, 'title': case['title'], 'group': case['group'],
                    'passed': bool(body.get('passed')), 'outcome': str(body.get('outcome', ''))[:40], 'checks': body.get('checks', [])[:30],
                    'seconds': body.get('seconds'), 'chat_id': str(body.get('chat_id', ''))[:64], 'at': body.get('at')})
    return {'ok': True}


@app.get('/api/runs')
def api_runs():
    return list(reversed(store.load_runs()))  # newest first


# ---- the chat page talks to these, exactly as it does on the real app ----
@app.post('/api/chat')
async def chat(request: Request):
    client = httpx.AsyncClient(timeout=None)
    try:
        upstream = await client.send(client.build_request('POST', APP_URL + '/api/chat', content=await request.body(), headers=await app_headers({'Content-Type': 'application/json'})), stream=True)
    except httpx.HTTPError:
        await client.aclose()
        message = f'The app under test is not reachable at {APP_URL}. Start it, then run the case again.'
        return StreamingResponse(iter([sse({'type': 'error', 'message': message}), sse({'type': 'done', 'waiting': False})]), media_type='text/event-stream')

    async def pump():
        try:
            async for chunk in upstream.aiter_raw():
                yield chunk
        finally:
            await upstream.aclose()
            await client.aclose()
    return StreamingResponse(pump(), status_code=upstream.status_code, media_type=upstream.headers.get('content-type', 'text/event-stream'))


@app.get('/api/thread/{thread_id}')
async def thread(thread_id: str):
    if not ID.match(thread_id):
        return JSONResponse({'error': 'Bad thread id.'}, status_code=400)
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            return Response((await client.get(f'{APP_URL}/api/thread/{thread_id}', headers=await app_headers())).content, media_type='application/json')
    except httpx.HTTPError:
        return {'exists': False, 'waiting': False}


@app.get('/api/app-chats')
@app.get('/api/app-chats/{chat_id}')
async def app_chats(chat_id: str = ''):
    """Chats typed straight into the app (read only), so the dashboard can show them next to the test runs."""
    if chat_id and not ID.match(chat_id):
        return JSONResponse({'error': 'Bad chat id.'}, status_code=400)
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            upstream = await client.get(f'{APP_URL}/api/chats' + (f'/{chat_id}' if chat_id else ''), headers=await app_headers())
        return Response(upstream.content, status_code=upstream.status_code, media_type='application/json')
    except httpx.HTTPError:
        return [] if not chat_id else JSONResponse({'error': 'App not reachable.'}, status_code=502)


def judge(log: list) -> list[dict]:
    """A chat typed in the app has no test case, so it is judged on what every chat should do."""
    evs = [i['ev'] for i in log if isinstance(i, dict) and 'ev' in i]
    scout = lambda e: (e.get('type') == 'sub' and e.get('kind') == 'result') or (e.get('type') == 'tool_result' and e.get('name') in ('scout_agent', 'parser_agent'))
    error = next((e for e in evs if e.get('type') == 'error'), None)
    asked, heard, last = 0, True, None  # every question to the person must follow a scout report that needed input
    for e in evs:
        if scout(e):
            last = (e.get('data') or {}).get('status')
        elif e.get('type') == 'tool_call' and e.get('name') in ('scout_agent', 'parser_agent'):
            asked += 1
        elif e.get('type') == 'interrupt':
            heard, last = heard and last == 'needs_input', None
    final = [e for e in evs if e.get('type') in ('message', 'interrupt')]
    reports = [(e.get('data') or {}).get('status') for e in evs if scout(e)]
    ended = bool(final) and (final[-1]['type'] == 'interrupt' or (bool(reports) and reports[-1] == 'ready'))
    return [{'ok': error is None, 'label': 'The coordinator answered without an error', 'detail': error and str(error.get('message', ''))[:80]},
            {'ok': asked > 0, 'label': 'The scout agent was asked'},
            {'ok': heard, 'label': 'Every question to the person came from the scout agent'},
            {'ok': ended, 'label': 'The chat ended with a ready scout report or a question to the person'}]


@app.get('/api/app-runs')
async def app_runs():
    """The chats typed into the app, shaped like test runs so the dashboard and the replay page treat them the same."""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            listing = (await client.get(f'{APP_URL}/api/chats', headers=await app_headers())).json()
    except (httpx.HTTPError, ValueError):
        return []
    out = []
    async with httpx.AsyncClient(timeout=10) as client:
        for c in listing:
            try:
                log = (await client.get(f'{APP_URL}/api/chats/{c["id"]}', headers=await app_headers())).json().get('log', [])
            except (httpx.HTTPError, ValueError):
                log = []
            checks = judge(log)
            passed = all(k['ok'] for k in checks)
            out.append({'run_id': c['id'], 'case_id': c['id'], 'title': c['title'], 'group': 'app', 'passed': passed,
                        'outcome': 'passed' if passed else 'error' if not checks[0]['ok'] else 'failed', 'checks': checks,
                        'seconds': None, 'chat_id': c['id'], 'at': c['updated']})
    return out


@app.get('/api/chats')
async def chats_list():
    mine = store.list_chats()  # test chats, plus the app's own chats so the replay page can open them too
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            theirs = (await client.get(f'{APP_URL}/api/chats', headers=await app_headers())).json()
    except (httpx.HTTPError, ValueError):
        theirs = []
    known = {c['id'] for c in mine}
    return sorted(mine + [c for c in theirs if c['id'] not in known], key=lambda c: c['updated'], reverse=True)


@app.get('/api/chats/{chat_id}')
async def chats_get(chat_id: str):
    chat = store.load_chat(chat_id) if ID.match(chat_id) else None
    if chat is None and ID.match(chat_id):  # not a test chat: it may be one typed in the app
        reply = await app_chats(chat_id)
        return reply if getattr(reply, 'status_code', 200) == 200 else JSONResponse({'error': 'Chat not found.'}, status_code=404)
    return chat or JSONResponse({'error': 'Chat not found.'}, status_code=404)


@app.put('/api/chats/{chat_id}')
async def chats_put(chat_id: str, request: Request):
    try:
        body = await request.json()
        title, updated, log = str(body['title']), float(body['updated']), body['log']
        if not ID.match(chat_id) or not isinstance(log, list):
            raise ValueError('bad chat')
        store.save_chat(chat_id, title, updated, log)
    except (ValueError, KeyError, TypeError):
        return JSONResponse({'error': 'Send {title, updated, log} for a valid chat id.'}, status_code=400)
    return {'ok': True}


@app.delete('/api/chats/{chat_id}')
def chats_delete(chat_id: str):
    if not ID.match(chat_id):
        return JSONResponse({'error': 'Bad chat id.'}, status_code=400)
    store.delete_chat(chat_id)
    return {'ok': True}


# ---- pages ----
# ---- Eval 1 (scout agent: stated details reported as missing): the person labels each scout call pass / fail ----
EVAL_DIR = HERE.parent / 'evals suit' / 'scout_fields'
LABEL_OK = {'pass', 'fail', 'unsure'}
REPORT_KEYS = ('status', 'business_type', 'area', 'city', 'missing', 'question')


def _read(name: str, default):
    try:
        return json.loads((EVAL_DIR / name).read_text('utf-8'))
    except (OSError, ValueError):
        return default


def _key(request, report: dict) -> str:
    text = json.dumps([request, {k: report.get(k) for k in REPORT_KEYS}], ensure_ascii=False, sort_keys=True)
    return hashlib.sha1(text.encode('utf-8')).hexdigest()[:12]


@app.get('/api/eval/scout-fields')
def eval_scout_fields():
    """Scout calls with a report, identical ones merged. The eval's own verdict is sent only for items already labelled,
    so the person labels first and is not steered by it."""
    labels = _read('labels.json', {})
    results = {r['observation_id']: r for r in _read('eval_results.json', [])}
    items, no_report = {}, 0
    for c in _read('scout_calls.json', []):
        if not isinstance(c.get('report'), dict):
            no_report += 1
            continue
        key = _key(c['request'], c['report'])
        item = items.setdefault(key, {'key': key, 'request': c['request'], 'report': {k: c['report'].get(k) for k in REPORT_KEYS},
                                      'count': 0, 'sessions': [], 'eval': results.get(c['observation_id'])})
        item['count'] += 1
        if c['session_id'] and c['session_id'] not in item['sessions']:
            item['sessions'].append(c['session_id'])
    out = []
    for item in items.values():
        label = labels.get(item['key'])
        item['label'] = label
        if not label:
            item['eval'] = None
        elif item['eval']:
            item['eval'] = {k: item['eval'].get(k) for k in ('verdict', 'failures', 'notes', 'reference')}
        out.append(item)
    return {'items': out, 'no_report': no_report, 'ran': bool(results)}


@app.post('/api/eval/scout-fields/label')
async def eval_scout_fields_label(request: Request):
    body = await request.json()
    key, label, note = str(body.get('key', '')), body.get('label'), str(body.get('note', ''))[:500]
    if not re.match(r'^[0-9a-f]{12}$', key) or (label is not None and label not in LABEL_OK):
        return JSONResponse({'error': 'Bad label.'}, status_code=400)
    labels = _read('labels.json', {})
    if label is None:
        labels.pop(key, None)
    else:
        labels[key] = {'label': label, 'note': note, 'at': time.time()}
    tmp = EVAL_DIR / 'labels.json.tmp'
    tmp.write_text(json.dumps(labels, ensure_ascii=False, indent=1), 'utf-8')
    tmp.replace(EVAL_DIR / 'labels.json')
    return {'ok': True}


# ---- Eval dashboard: one row per session, one column per eval. To add an eval: write its drawer-items function and runner, then add one line to EVALS. ----
HANDOFF_DIR = HERE.parent / 'evals suit' / 'coordinator_handoff'
ASK_DIR = HERE.parent / 'evals suit' / 'coordinator_ask'
SCOUT_CALL_DIR = HERE.parent / 'evals suit' / 'coordinator_scout_call'
EVAL_QUEUE: queue.Queue = queue.Queue()
EVAL_STATE: dict[str, str] = {}  # 'eval id:session id' -> 'queued' | 'running' | 'error: ...'; absent when idle


def _read_in(folder: Path, name: str, default):
    try:
        return json.loads((folder / name).read_text('utf-8'))
    except (OSError, ValueError):
        return default


def _run_scout_fields(sid: str):
    import eval_core
    eval_core.run_session(sid)


def _run_handoff(sid: str):
    import handoff_eval
    handoff_eval.run_session(sid)


def _run_ask(sid: str):
    import ask_eval
    ask_eval.run_session(sid)


def _run_scout_call(sid: str):
    import scout_call_eval
    scout_call_eval.run_session(sid)


def _verdict(calls: list[dict]) -> str:
    verdicts = {c['verdict'] for c in calls}
    if not calls:
        return 'no_calls'
    # a pass needs every call checked: one call without a result keeps the session at "unknown"
    return 'fail' if 'fail' in verdicts else 'review' if 'review' in verdicts else 'unknown' if 'unknown' in verdicts else 'pass'


def _val(v):
    return 'null' if v in (None, '') else ', '.join(v) if isinstance(v, list) and v else '[ ]' if isinstance(v, list) else str(v)


def _items_scout_fields(calls: list[dict]) -> list[dict]:
    """One drawer item per scout call, in the shape every eval uses: verdict, heading, the text, lines of detail, reasons."""
    items = []
    for i, c in enumerate(calls, 1):
        r, ref = c.get('report'), c.get('reference')
        lines = [('Scout agent reported: business_type %s · area %s · city %s · missing %s · %s' % (_val(r.get('business_type')), _val(r.get('area')), _val(r.get('city')), _val(r.get('missing')), r.get('status')))
                 if r else 'The scout agent returned no report (it failed before answering).']
        if ref:
            lines.append('The person gave: business_type %s · area %s · city %s' % (_val(ref.get('business_type')), _val(ref.get('area')), _val(ref.get('city'))))
        if c.get('fields'):
            lines.append('Per detail: ' + ' · '.join(f'{k} {v}' for k, v in c['fields'].items()))
        items.append({'verdict': c['verdict'], 'heading': 'First scout call' + (f' ({", ".join(c["failures"])})' if c.get('failures') else ''),
                      'label': 'Message the coordinator sent to the scout agent', 'text': c['request'], 'lines': lines, 'why': list(c.get('notes') or []),
                      'memory': c.get('memory'), 'memory_label': "The conversation the extractor read (only the person's side)"})
    return items


def _items_handoff(calls: list[dict]) -> list[dict]:
    items = []
    for i, c in enumerate(calls, 1):
        items.append({'verdict': c['verdict'], 'heading': f'Call {i}' + (f' ({c["failure_type"]})' if c.get('failure_type') not in (None, 'none') else ''),
                      'label': 'Request the coordinator wrote', 'text': c['request'], 'lines': [], 'memory': c.get('memory'),
                      'why': [c['critique']] if c.get('critique') else []})
    return items


def _items_ask(calls: list[dict]) -> list[dict]:
    """One drawer item per message the coordinator sent to the person."""
    names = {'off_topic': 'about something else', 'asks_known': 'asks what the person already said', 'unhelpful': 'does not help', 'no_results': 'does not show the results'}
    heads = {'ask': 'Question to the person', 'reply': 'Reply to the person'}
    items = []
    for c in calls:
        fails = f' ({", ".join(c["failures"])})' if c.get('failures') else ''
        lines = []
        if c.get('outcome'):
            lines.append('The scout agent had finished its search: ' + ('it found places' if c['outcome'] == 'results' else 'it reported a problem'))
        if c.get('scores'):
            lines.append('Chance of yes from the Decisions API (a yes is a problem): ' + ' · '.join(f'{names.get(k, k)} {round(v * 100)}%' for k, v in c['scores'].items()))
        items.append({'verdict': c['verdict'], 'heading': heads.get(c.get('channel'), 'Message') + fails, 'label': 'What the coordinator said to the person', 'text': c['message'],
                      'lines': lines, 'why': list(c.get('notes') or []), 'memory': c.get('memory'), 'memory_label': 'The conversation when it was written'})
    return items


def _items_scout_call(calls: list[dict]) -> list[dict]:
    """One drawer item per coordinator model call: what it was reacting to, whether it should have called the scout agent, and what it did."""
    reacting = {'person_message': "the person's message", 'person_answer': "the person's answer to a question", 'scout_report': "the scout agent's report"}
    names = {'not_called': 'did not call the scout agent', 'called_wrongly': 'called the scout agent when it should not'}
    items = []
    for i, c in enumerate(calls, 1):
        latest = f'status {c["latest"]}' if c['trigger'] == 'scout_report' else f'"{c["latest"]}"'
        should = {'yes': 'Yes', 'no': 'No'}.get(c.get('expected'), 'Could not be decided')
        by = 'by rule' if c.get('source') == 'rule' else 'by the judge model'
        fails = f' ({names.get(c["failure_type"], c["failure_type"])})' if c.get('failure_type') not in (None, 'none') else ''
        items.append({'verdict': c['verdict'], 'heading': f'Model call {i}{fails}', 'label': 'What the coordinator did', 'text': c['did'],
                      'lines': [f'It was reacting to {reacting.get(c["trigger"], "something else")}: {latest}',
                                f'Should it call the scout agent here? {should} ({by})',
                                'Did it call the scout agent? ' + ('Yes' if c['called_scout'] else 'No') + ' (read from the Langfuse trace)'],
                      'why': [c['critique']] if c.get('critique') else [], 'memory': c.get('memory'), 'memory_label': 'The conversation the coordinator had at that moment'})
    return items


# Each eval: id, number in evals.md, title, subtitle, its folder, how to evaluate one session, its verdict-per-session file,
# its result-per-call file, and how results become drawer items. Optional: unit (what one result is called) and empty (the drawer text when there is nothing).
EVALS = [
    {'id': 'scout_fields', 'num': 1, 'title': 'Stated details', 'subtitle': "Scout agent reports the business, area and city from the person's first query",
     'scope': "Checks only the scout agent's first call of the session (the one for the person's first query)",
     'dir': EVAL_DIR, 'run': _run_scout_fields, 'runs_file': 'session_runs.json', 'results_file': 'eval_results.json', 'items': _items_scout_fields},
    {'id': 'ask', 'num': 2, 'title': 'Helpful replies', 'subtitle': 'Every message the coordinator sends the person helps with what they asked', 'unit': 'message',
     'scope': "Checks every message the coordinator sent to the person: its questions and its final reply. Its thinking is not checked",
     'empty': 'The coordinator never spoke to the person, so there is nothing to check.',
     'dir': ASK_DIR, 'run': _run_ask, 'runs_file': 'ask_sessions.json', 'results_file': 'ask_results.json', 'items': _items_ask},
    {'id': 'handoff', 'num': 3, 'title': 'Hand-off arguments', 'subtitle': 'Coordinator passes everything the person said to the scout agent',
     'scope': 'Checks every scout agent call of the session',
     'dir': HANDOFF_DIR, 'run': _run_handoff, 'runs_file': 'handoff_sessions.json', 'results_file': 'handoff_results.json', 'items': _items_handoff},
    {'id': 'scout_call', 'num': 4, 'title': 'Scout agent called', 'subtitle': 'Coordinator calls the scout agent whenever the person is searching for a place', 'unit': 'model call',
     'scope': 'Checks every model call of the coordinator: should it have called the scout agent at that point, and did it',
     'empty': 'No model call of the coordinator was found in Langfuse for this session, so there is nothing to check.',
     'dir': SCOUT_CALL_DIR, 'run': _run_scout_call, 'runs_file': 'scout_call_sessions.json', 'results_file': 'scout_call_results.json', 'items': _items_scout_call},
]
EVAL_BY_ID = {e['id']: e for e in EVALS}


def _eval_worker():
    """Runs queued (eval, session) pairs one at a time: the judge models allow only a few requests a minute."""
    while True:
        eval_id, sid = EVAL_QUEUE.get()
        key = f'{eval_id}:{sid}'
        EVAL_STATE[key] = 'running'
        try:
            for folder in {str(EVAL_BY_ID[eval_id]['dir']), str(EVAL_DIR)}:
                if folder not in sys.path:
                    sys.path.insert(0, folder)
            EVAL_BY_ID[eval_id]['run'](sid)
            EVAL_STATE.pop(key, None)
        except Exception as error:
            EVAL_STATE[key] = f'error: {type(error).__name__} {str(error)[:140]}'


threading.Thread(target=_eval_worker, daemon=True).start()


def _cell(ev: dict, sid: str, runs: dict, by_session: dict) -> dict:
    state = EVAL_STATE.get(f"{ev['id']}:{sid}")
    if state in ('queued', 'running'):
        return {'state': state}
    evaluated = sid in runs  # only a Run from this dashboard counts
    cell = {'state': 'done' if evaluated else 'not_run'}
    if state:  # the last run failed
        cell['error'] = state
    if evaluated:
        calls = by_session.get(sid, [])
        info = runs.get(sid) or {}
        verdict = info.get('verdict') if ev['id'] in ('handoff', 'scout_call') else _verdict(calls)  # these evals save their own verdict per session
        cell.update(verdict={'no scout call': 'no_calls', 'no coordinator call': 'unknown'}.get(verdict, verdict), ran_at=info.get('ran_at'), count=len(calls), items=ev['items'](calls))
    return cell


# ---- Production sessions: the chats of the deployed app. Its chats live on the host (and are lost when a free instance restarts), so they are read
# from Langfuse, where the app files every trace and session under the environment "production". The evals read Langfuse too, so they run on these as they are. ----
PROD_ENV = os.getenv('EVAL_ENVIRONMENT', 'production')
PROD_TTL = 60  # seconds before the list is read again; the read takes several seconds, so it runs in the background
PROD: dict = {'rows': [], 'at': 0.0, 'busy': False, 'error': None, 'host': ''}


def _refresh_production():
    try:
        if str(EVAL_DIR) not in sys.path:
            sys.path.insert(0, str(EVAL_DIR))
        import eval_core
        PROD['rows'], PROD['host'], PROD['error'] = eval_core.list_sessions(PROD_ENV), eval_core.LANGFUSE, None  # on a failure the last list stays
    except Exception as error:
        PROD['error'] = f'{type(error).__name__}: {str(error)[:140]}'
    finally:
        PROD['at'], PROD['busy'] = time.time(), False


def production_state(force: bool = False) -> dict:
    if not PROD['busy'] and (force or time.time() - PROD['at'] > PROD_TTL):
        PROD['busy'] = True
        threading.Thread(target=_refresh_production, daemon=True).start()
    return PROD


@app.get('/api/eval/sessions')
async def eval_sessions(refresh: int = 0):
    chats = await chats_list()
    mine = {c['id'] for c in store.list_chats()}
    prod = production_state(bool(refresh))
    known = {c['id'] for c in chats}
    # a chat's "updated" is in milliseconds (the page writes Date.now())
    chats = chats + [{'id': s['id'], 'title': s['title'], 'updated': s['updated'] * 1000} for s in prod['rows'] if s['id'] not in known]
    on_prod = {s['id']: s for s in prod['rows']}
    per_eval = {}
    for ev in EVALS:
        by_session: dict[str, list] = {}
        for r in _read_in(ev['dir'], ev['results_file'], []):
            by_session.setdefault(r['session_id'], []).append(r)
        per_eval[ev['id']] = (_read_in(ev['dir'], ev['runs_file'], {}), by_session)
    def row(c):
        s = on_prod.get(c['id'])
        extra = {'source': 'production', 'turns': s['turns'], 'url': f"{prod['host']}/project/{s['project_id']}/sessions/{s['id']}"} if s else {'source': 'test' if c['id'] in mine else 'app'}
        return {'id': c['id'], 'title': c['title'], 'updated': c['updated'], **extra, 'cells': {ev['id']: _cell(ev, c['id'], *per_eval[ev['id']]) for ev in EVALS}}
    rows = sorted((row(c) for c in chats), key=lambda r: r['updated'], reverse=True)
    return {'evals': [{k: e.get(k) for k in ('id', 'num', 'title', 'subtitle', 'scope', 'unit', 'empty')} for e in EVALS], 'rows': rows, 'queue': EVAL_QUEUE.qsize(),
            'production': {'environment': PROD_ENV, 'count': len(prod['rows']), 'loading': prod['at'] == 0 and prod['busy'], 'error': prod['error'], 'at': prod['at']}}


@app.post('/api/eval/{eval_id}/run')
async def eval_run(eval_id: str, request: Request):
    """Queue one eval for the given sessions (the page sends one, or all the ones not yet run)."""
    if eval_id not in EVAL_BY_ID:
        return JSONResponse({'error': 'Unknown eval.'}, status_code=404)
    ids = [str(i) for i in (await request.json()).get('session_ids', [])][:200]
    if not ids or not all(ID.match(i) for i in ids):
        return JSONResponse({'error': 'Bad session id.'}, status_code=400)
    queued = []
    for sid in ids:
        key = f'{eval_id}:{sid}'
        if EVAL_STATE.get(key) in ('queued', 'running'):
            continue
        EVAL_STATE[key] = 'queued'
        EVAL_QUEUE.put((eval_id, sid))
        queued.append(sid)
    return {'queued': queued}


def page(name: str) -> Response:
    html = (HERE / name).read_text('utf-8').replace('__APP_URL__', APP_URL)  # the link from the simulator to the app
    return Response(html, media_type='text/html; charset=utf-8')


def app_page(script: str) -> Response:
    html = (HERE / 'ui' / 'index.html').read_text('utf-8')  # the app's page, byte for byte, plus one script of ours
    return Response(html.replace('</head>', f'<script src="{script}"></script></head>', 1), media_type='text/html; charset=utf-8')


@app.get('/')
def dashboard():
    return page('dashboard.html')


@app.get('/evals')
def evals_page():
    return page('evals.html')


@app.get('/runs')
def runs():
    return page('runs.html')


@app.get('/run/{case_id}')
def run(case_id: str):
    if not CASE_ID.match(case_id) or case_id not in {c['id'] for c in cases()}:
        return JSONResponse({'error': 'Unknown test case.'}, status_code=404)
    return app_page('/sim.js')


@app.get('/chat/{chat_id}')
def saved_chat(chat_id: str):
    if not ID.match(chat_id):
        return JSONResponse({'error': 'Bad chat id.'}, status_code=400)
    return app_page('/replay.js')  # opens a saved test chat, read-only


@app.get('/timeline/{chat_id}')
def timeline(chat_id: str):
    if not ID.match(chat_id):
        return JSONResponse({'error': 'Bad chat id.'}, status_code=400)
    return page('timeline.html')  # who said what, step by step


@app.get('/{path:path}')
def static_file(path: str):
    route = '/' + path
    if route not in STATIC:
        return JSONResponse({'error': 'Not found'}, status_code=404)
    name, mime = STATIC[route]
    return Response((HERE / name).read_bytes(), media_type=f'{mime}; charset=utf-8')


if __name__ == '__main__':
    print(f'Scout simulator: http://127.0.0.1:8100   (app under test: {APP_URL})')
    uvicorn.run(app, host='127.0.0.1', port=int(os.getenv('SIM_PORT', '8100')), log_level='warning')
