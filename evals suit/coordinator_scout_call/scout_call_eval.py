"""Eval 4 (coordinator -> scout agent): at every step, did the coordinator call the scout agent when it should, and only then?

Why: the coordinator must hand every place search to the scout agent. In session 427742b37e884a4e980855e971ce2f74 the person had given the
business type, the area and the city, yet the coordinator asked them again and finally only replied in text. It never called the scout agent
again. Eval 3 judges the scout calls that were made, so a call that never happened is not seen by it. This eval looks for those.

For EVERY coordinator model call in a Langfuse session (the observations named "Coordinator: model call"):
  1. What the coordinator was reacting to is read from the conversation it was given (the model call's input in Langfuse). The LAST thing in it is
       person_message   the person's message          -> the judge model decides: is this about finding places?
       person_answer    the person's answer to a question the coordinator relayed -> the judge model decides: does it continue a place search?
       scout_report     the scout agent's report had just come back -> the coordinator must relay it (ask the person, or reply), so no
                        new scout call is expected. Decided by rule, no model is asked.
  2. What the coordinator did is read from the same model call's output in Langfuse: did it call scout_agent?
  3. Compare the two:
       should call + called          PASS
       should call + did not         FAIL  not_called      (it asked the person or replied in text instead of handing over)
       should not call + did not     PASS
       should not call + called      FAIL  called_wrongly  (a scout search for something that is not a place search, or a second search
                                                            where the coordinator had to relay the report)
A session PASSES only if every one of its coordinator model calls passes. One failing call fails the session.

The judge sees the conversation up to that call and nothing the coordinator did afterwards, so it cannot lean on the coordinator's choice. It
answers one question ("should the coordinator call the scout agent now?") with a critique first. Its prompt lives in prompts/scout-call-judge.md
and in Langfuse prompt management (name scout-call-judge); sync_prompt.py saves it there. scout_call_calibrate.py checks the judge against
labelled cases (scout_call_cases.json).

usage:
  python scout_call_eval.py <session_id> [<session_id> ...]
  python scout_call_eval.py --all            every session that has a coordinator model call (Langfuse)
  python scout_call_eval.py --all --missing  the same, skipping sessions already evaluated (to carry on after the judge model's limit stopped a run)
  python scout_call_eval.py <session_id> --show   print the conversation and critique of every call, not only of the failures

Files here: scout_call_results.json (one result per coordinator model call), scout_call_sessions.json (one verdict per session).
Nothing is cached: every run asks the judge again.
"""
import json, sys, time
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE.parent / 'scout_fields'))
sys.path.insert(0, str(HERE.parent / 'coordinator_handoff'))
import eval_core  # Langfuse access and the judge model, shared with the other evals
from handoff_eval import render_memory  # the coordinator's saved conversation as plain lines

COORDINATOR_CALL = 'Coordinator: model call'  # the name the app gives every model call of the coordinator (app/coordinator.py)
_prompt = None


def judge_prompt():
    """The judge prompt from Langfuse prompt management (name scout-call-judge, label draft). Save the file there with sync_prompt.py."""
    global _prompt
    if _prompt is None:
        from langfuse import Langfuse
        client = Langfuse(public_key=eval_core.ENV['LANGFUSE_PUBLIC_KEY'], secret_key=eval_core.ENV['LANGFUSE_SECRET_KEY'], base_url=eval_core.LANGFUSE)
        _prompt = client.get_prompt('scout-call-judge', type='text', label=eval_core.PROMPT_LABEL, cache_ttl_seconds=0)
    return _prompt


def _read(name, default):
    try:
        return json.loads((HERE / name).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return default


def _write(name, data):
    tmp = HERE / (name + '.tmp')
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding='utf-8')
    tmp.replace(HERE / name)


def _text(content):
    return content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)


# ---- what the coordinator was reacting to -------------------------------------------------------------------------------------------------
def trigger_of(messages):
    """(kind, latest) for the LAST thing in the conversation the coordinator was given.
    The system prompt and the tool definitions (tool messages with a dict as content) do not count: Langfuse lists the definitions after the chat."""
    for m in reversed(messages or []):
        role, content = m.get('role'), m.get('content')
        if role == 'system' or (role == 'tool' and isinstance(content, dict)):
            continue
        if role == 'user':
            return 'person_message', _text(content)
        if role == 'tool' and m.get('name') == 'ask_user':
            return 'person_answer', _text(content)
        if role == 'tool' and m.get('name') == 'scout_agent':
            report = eval_core._parse(content)
            return 'scout_report', report.get('status') if isinstance(report, dict) else None
        return 'other', None  # an assistant message last: the coordinator is continuing its own turn
    return 'other', None


# ---- what the coordinator did -------------------------------------------------------------------------------------------------------------
def action_of(output):
    """(called_scout, a plain description of what the model call did) from the model call's output in Langfuse."""
    calls = output.get('tool_calls') or []
    for c in calls:
        if c.get('name') == 'scout_agent':
            return True, f'Called the scout agent: "{(c.get("args") or {}).get("request")}"'
    for c in calls:
        if c.get('name') == 'ask_user':
            return False, f'Asked the person: "{(c.get("args") or {}).get("question")}"'
    if calls:
        return False, 'Called ' + ', '.join(str(c.get('name')) for c in calls)
    text = _text(output.get('content') or '').strip()
    return False, f'Replied to the person in text: "{text}"' if text else 'Said nothing (an empty reply, no tool call)'


def pull_calls(session_id=None):
    """Every coordinator model call that has an output, oldest first, with what it was reacting to and what it did.
    A model call without an output (it failed and was retried) made no decision and is left out."""
    flt = [{'type': 'string', 'column': 'name', 'operator': '=', 'value': COORDINATOR_CALL}]
    if session_id:
        flt.append({'type': 'string', 'column': 'sessionId', 'operator': '=', 'value': session_id})
    calls = []
    for o in sorted(eval_core._paged(flt), key=lambda o: o['startTime']):
        inp, out = eval_core._parse(o.get('input')), eval_core._parse(o.get('output'))
        if not isinstance(inp, list) or not isinstance(out, dict):
            continue
        kind, latest = trigger_of(inp)
        called, did = action_of(out)
        calls.append({'call_id': o['id'], 'session_id': o.get('sessionId'), 'time': o['startTime'], 'trigger': kind, 'latest': latest,
                      'memory': render_memory(inp), 'did': did, 'called_scout': called})
    return calls


# ---- should it have called? ---------------------------------------------------------------------------------------------------------------
def expected_call(call, ask=None):
    """(answer, source, critique): 'yes' or 'no' (None when the judge could not answer), who decided it, and why.
    A scout report that just came back is decided by rule; a person's words are decided by the judge model."""
    if call['trigger'] == 'scout_report':
        return 'no', 'rule', (f'The scout agent had just reported (status {call["latest"]}). The coordinator\'s job now is to relay it: ask the person the '
                              'question the scout agent needs answered, or tell them what was found, or what went wrong. A new scout search is not expected here.')
    if call['trigger'] not in ('person_message', 'person_answer'):
        return None, 'rule', 'The conversation does not end with the person or the scout agent, so there is nothing to decide.'
    prompt = judge_prompt()
    read = (ask or eval_core._ask)(prompt.compile(memory=call['memory'], latest=call['latest'], trigger=call['trigger']))
    answer = str((read or {}).get('should_call_scout', '')).strip().lower()
    return (answer if answer in ('yes', 'no') else None), f'judge {eval_core.MODEL}, prompt v{prompt.version}', (read or {}).get('critique')


def verdict_of(expected, called):
    """(verdict, failure_type) from what was expected and what happened."""
    if expected is None:
        return 'unknown', None
    if expected == 'yes':
        return ('pass', 'none') if called else ('fail', 'not_called')
    return ('fail', 'called_wrongly') if called else ('pass', 'none')


def judge(call, ask=None):
    expected, source, critique = expected_call(call, ask)
    verdict, failure = verdict_of(expected, call['called_scout'])
    return {**call, 'expected': expected, 'source': source, 'critique': critique, 'verdict': verdict, 'failure_type': failure}


def session_verdict(results):
    """pass only when every coordinator model call passed; one fail fails the session; a call the judge could not answer makes it unknown."""
    if not results:
        return 'no coordinator call'
    if any(r['verdict'] == 'fail' for r in results):
        return 'fail'
    return 'pass' if all(r['verdict'] == 'pass' for r in results) else 'unknown'


def run_session(session_id, ask=None):
    """Judge every coordinator model call of one session and save the results. Returns the per-call results."""
    results = [judge(c, ask) for c in pull_calls(session_id)]
    merged = {r['call_id']: r for r in _read('scout_call_results.json', [])}
    merged.update({r['call_id']: r for r in results})
    _write('scout_call_results.json', sorted(merged.values(), key=lambda r: r['time']))
    sessions = _read('scout_call_sessions.json', {})
    sessions[session_id] = {'verdict': session_verdict(results), 'calls': len(results), 'failed_calls': sum(r['verdict'] == 'fail' for r in results),
                            'ran_at': time.time(), 'judge_model': eval_core.MODEL, 'prompt': f'v{judge_prompt().version}'}
    _write('scout_call_sessions.json', sessions)
    return results


def run(session_ids, show=False):
    summary = {}
    for sid in session_ids:
        try:
            results = run_session(sid)
        except RuntimeError as error:  # the judge model's limit: what was done is saved, run again later with --missing
            print(f'\nstopped at session {sid}: {error}')
            break
        verdict = session_verdict(results)
        summary[sid] = verdict
        print(f'\n{verdict.upper():18} session {sid}  ({len(results)} coordinator model call(s))')
        for i, r in enumerate(results, 1):
            want = {'yes': 'should call', 'no': 'no call needed', None: 'undecided'}[r['expected']]
            print(f'   call {i}: {r["verdict"].upper():7} {r["failure_type"] or "":14} after {r["trigger"]:14} {want:15} {r["did"][:90]}')
            if show or r['verdict'] != 'pass':
                print('      conversation:\n         ' + r['memory'].replace('\n', '\n         '))
                print(f'      decided by {r["source"]}: {r["critique"]}')
    return summary


if __name__ == '__main__':
    args = [a for a in sys.argv[1:] if not a.startswith('--')]
    if '--all' in sys.argv:
        args = sorted({c['session_id'] for c in pull_calls() if c['session_id']})
        if '--missing' in sys.argv:
            done = _read('scout_call_sessions.json', {})
            args = [a for a in args if a not in done]
    if not args:
        sys.exit(__doc__)
    summary = run(args, show='--show' in sys.argv)
    print('\n', {v: sum(1 for s in summary.values() if s == v) for v in sorted(set(summary.values()))})
