"""list_sessions turns Langfuse's rows into one entry per chat session (no network).  python test_list_sessions.py"""
from unittest import mock

import eval_core


def run_row(trace, session, at):  # the "Coordinator" observation: it carries the session id
    return {'name': 'Coordinator', 'traceId': trace, 'sessionId': session, 'startTime': at, 'projectId': 'proj'}


def root_row(trace, name, at):  # the root "Turn N ..." observation: it has the text the person typed, but no session id
    return {'name': name, 'traceId': trace, 'sessionId': None, 'startTime': at, 'projectId': 'proj'}


RUNS = [run_row('t1', 'sess-a', '2026-11-18T08:38:31.421Z'), run_row('t2', 'sess-a', '2026-11-18T08:38:48.000Z'),
        run_row('t3', 'sess-b', '2026-11-18T09:00:00.000Z'), run_row('t4', None, '2026-11-18T09:05:00.000Z')]
ROOTS = [root_row('t2', 'Turn 2 (HITL answer): satyanarayanapuram', '2026-11-18T08:38:48.000Z'),
         root_row('t1', 'Turn 1 (message): dentist near me', '2026-11-18T08:38:31.421Z'),
         root_row('t3', 'Turn 4 (HITL answer): kothapet', '2026-11-18T09:00:00.000Z')]


def fake_paged(flt, fields='core,basic,io'):
    return RUNS if any(f['value'] == 'Coordinator' for f in flt) else ROOTS


with mock.patch.object(eval_core, '_paged', fake_paged):
    got = eval_core.list_sessions('production')

checks = [
    ('newest session first', [s['id'] for s in got], ['sess-b', 'sess-a']),
    ('the title is the first turn the person typed', got[1]['title'], 'dentist near me'),
    ('a session whose first turn is not in the window is named by its id', got[0]['title'], 'Session sess-b'),
    ('turns are counted per session', [s['turns'] for s in got], [1, 2]),
    ('a run without a session id is left out', len(got), 2),
    ('the time is the last turn, in seconds', got[1]['updated'], 1794991128.0),
    ('the project id is kept for the Langfuse link', got[0]['project_id'], 'proj'),
]
bad = 0
for name, value, want in checks:
    bad += value != want
    print('ok  ' if value == want else 'FAIL', name, '' if value == want else f'-> {value!r} (wanted {want!r})')
raise SystemExit(bad > 0)
