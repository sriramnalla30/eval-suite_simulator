"""Does the eval 4 judge agree with a person? Runs the judge on labelled cases and reports TPR and TNR (see validate-evaluator in CLAUDE_TEM_STORAGE).

scout_call_cases.json holds the cases: a conversation, what the person said last, and the label (yes = the coordinator should call the scout agent,
no = it should not). Each case has a split:
  train  the examples written into the judge prompt. Never scored: a judge shown the answer would flatter itself.
  dev    used again and again while the prompt is being tuned.
  test   looked at ONCE at the end, to report the final numbers. Do not tune the prompt on its misses.
The labels were written from the coordinator's own rules (prompts/coordinator.md). Cases marked "borderline" are judgement calls.

  TPR = of the cases labelled yes, how many the judge also says yes      (a low TPR means it misses place searches)
  TNR = of the cases labelled no,  how many the judge also says no       (a low TNR means it sends non-searches to the scout agent)
Target: both above 90% on dev; the minimum acceptable is 80%.

usage:
  python scout_call_calibrate.py                run the dev split
  python scout_call_calibrate.py --split test   run the test split (once)
  python scout_call_calibrate.py --split all    dev and test together
Result: scout_call_calibration_<split>.json (one row per case, overwritten on every run of that split).
"""
import json, sys, time
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
import scout_call_eval as ev


def load_cases():
    return json.loads((HERE / 'scout_call_cases.json').read_text(encoding='utf-8'))


def calibrate(split='dev', ask=None, on_each=None):
    """Judge every case of the split. Returns (rows, summary); each row says what the person labelled and what the judge answered."""
    cases = [c for c in load_cases() if (c['split'] in ('dev', 'test') if split == 'all' else c['split'] == split)]
    rows = []
    for c in cases:
        answer, source, critique = ev.expected_call(c, ask)
        row = {'id': c['id'], 'split': c['split'], 'label': c['label'], 'judge': answer, 'borderline': c.get('borderline', False), 'latest': c['latest'],
               'trigger': c['trigger'], 'critique': critique, 'agree': answer == c['label']}
        rows.append(row)
        if on_each:
            on_each(row)
    yes, no = [r for r in rows if r['label'] == 'yes'], [r for r in rows if r['label'] == 'no']
    summary = {'split': split, 'cases': len(rows), 'labelled_yes': len(yes), 'labelled_no': len(no),
               'tpr': sum(r['judge'] == 'yes' for r in yes) / len(yes) if yes else None,
               'tnr': sum(r['judge'] == 'no' for r in no) / len(no) if no else None,
               'undecided': sum(r['judge'] is None for r in rows), 'judge_model': ev.eval_core.MODEL, 'prompt': f'v{ev.judge_prompt().version}', 'ran_at': time.time()}
    return rows, summary


def pct(x):
    return 'n/a' if x is None else f'{x * 100:.0f}%'


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    split = sys.argv[sys.argv.index('--split') + 1] if '--split' in sys.argv else 'dev'
    if split not in ('dev', 'test', 'all'):
        sys.exit(__doc__)

    def show(r):
        print(f'{"ok  " if r["agree"] else "MISS"} {r["id"]:34} label={r["label"]:3} judge={r["judge"]}{"  (borderline)" if r["borderline"] else ""}', flush=True)
    rows, summary = calibrate(split, on_each=show)
    (HERE / f'scout_call_calibration_{split}.json').write_text(json.dumps({'summary': summary, 'rows': rows}, ensure_ascii=False, indent=1), encoding='utf-8')
    print(f'\n{summary["split"]}: {summary["cases"]} cases  TPR {pct(summary["tpr"])}  TNR {pct(summary["tnr"])}  undecided {summary["undecided"]}  prompt {summary["prompt"]}  judge {summary["judge_model"]}')
    for r in rows:
        if not r['agree']:
            print(f'\n  MISS {r["id"]}: person labelled {r["label"]}, judge said {r["judge"]}\n    latest: {r["latest"]!r}\n    critique: {r["critique"]}')
