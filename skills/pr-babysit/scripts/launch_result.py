#!/usr/bin/env python3
"""One pr-babysit launch, condensed for its caller from the launch's saved Workflow output.

    launch_result.py --output FILE [--state-ref FILE:DIGEST] [--checkout DIR]

--output is the launch's Workflow output file. It is empty when the launch threw
before returning, and then --state-ref, the stateRef that launch was given, is
the one to continue from. --checkout adds the checkout's branch, HEAD and dirty
paths. Every CI failure carries the `key` a caller passes back as
acceptedFailures: [{ key, reason, scope }].

`result` keeps every top-level result field verbatim except history, observation
and state, which are condensed; receipts are kept verbatim. `blockers` lists what
the caller must settle before trusting or continuing the launch, `notes` what is
absent but harmless.
"""
import json
import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from facts import Parser, Unusable, git, report  # noqa: E402

CONDENSED = ('history', 'observation', 'state')
NOT_FIXING = re.compile(r'^cycle \d+: rig-side CI failure \(not fixing\)')
CUT = 300


def load_output(path):
    try:
        raw = Path(path).read_text(encoding='utf-8')
    except (OSError, UnicodeDecodeError) as e:
        raise Unusable(f'cannot read --output {path}: {getattr(e, "strerror", None) or e}')
    if not raw.strip():
        return None
    try:
        output = json.loads(raw)
    except ValueError as e:
        raise Unusable(f'--output {path} is not JSON: {e}')
    if not isinstance(output, dict):
        raise Unusable(f'--output {path} is not a Workflow output object')
    return output


def cut(text):
    text = ' '.join(str(text).split())
    return text if len(text) <= CUT else text[:CUT] + '…'


def elapsed(progress):
    """Seconds from the first agent's start to the last one's end, or None."""
    spans = [(a['startedAt'], a['startedAt'] + (a.get('durationMs') or 0)) for a in progress or []
             if a.get('type') == 'workflow_agent' and isinstance(a.get('startedAt'), (int, float))]
    return round((max(e for _, e in spans) - min(s for s, _ in spans)) / 1000) if spans else None


def receipts(actions):
    """Pushes, replies and the adoption the last cycle recorded, verbatim, and what about them blocks.

    A reply batch's `pass` is the workflow's own verdict that every comment it
    owed is settled, its detail naming any that is not; the receipts are evidence.
    """
    pushes, replies, blocking = [], [], []
    for lane in ('reviewPush', 'ciPush'):
        p = actions.get(lane)
        if p:
            pushes.append({'lane': lane, **p})
            if p.get('pass') is not True or p.get('committed') is None:
                blocking.append(f'uncertain publication: {lane}: {cut(p.get("detail", ""))}')
    for kind in ('refutedPosts', 'fixNotePosts', 'deferralPosts'):
        posts = actions.get(kind)
        if posts:
            replies += [{'batch': kind, **r} for r in posts.get('receipts') or []]
            if posts.get('pass') is not True:
                blocking.append(f'reply batch did not pass: {kind}: {cut(posts.get("detail", ""))}')
    adoption = actions.get('adoption')
    if adoption and adoption.get('publication') not in ('pushed', 'already-published', None):
        blocking.append(f'uncertain publication: adoption {adoption.get("from", "")[:8]}..{adoption.get("to", "")[:8]}: {adoption.get("publication")}')
    return {'pushes': pushes, 'replies': replies, 'adoption': adoption}, blocking


def ci_summary(ci):
    """Counts by verdict; complete rig-side or accepted failures as {cell, key} per check; every other failure verbatim."""
    failures = ci.get('realFailures') or []
    settled = lambda f: f.get('complete') is True and (f.get('accepted') or f.get('verdict') == 'rig-side')
    cells = {}
    for f in filter(settled, failures):
        cells.setdefault(f.get('check'), []).append({'cell': f.get('cell'), 'key': f.get('key')})
    return {'status': ci.get('status'), 'headSha': ci.get('headSha'), 'infraRerun': ci.get('infraRerun'),
            'verdicts': dict(Counter('accepted' if f.get('accepted') else f.get('verdict') for f in failures)),
            'settledCells': cells, 'attention': [f for f in failures if not settled(f)]}


def logs(lines):
    """Every log line cut to CUT, except that rig-side "not fixing" lines are counted: ci lists those failures."""
    lines = lines or []
    kept = [l if len(l) <= CUT else f'{l[:CUT]}… ({len(l)} chars; whole line in the output\'s logs)'
            for l in lines if not NOT_FIXING.match(l)]
    dropped = len(lines) - len(kept)
    return kept + ([f'({dropped} rig-side "not fixing" lines, one per failure listed under ci)'] if dropped else [])


def checkout(path):
    return {'branch': git('-C', path, 'rev-parse', '--abbrev-ref', 'HEAD').strip(),
            'head': git('-C', path, 'rev-parse', 'HEAD').strip(),
            'dirty': [l for l in git('-C', path, 'status', '--porcelain').splitlines() if l]}


def summarize(output, output_path, state_ref=None, tree=None):
    blockers, notes = [], []
    summary = {'launch': None, 'result': None, 'stateRef': None, 'observation': None, 'receipts': None,
               'logs': [], 'checkout': tree, 'blockers': blockers, 'notes': notes}
    result = output.get('result') if output else None
    if output is None:
        blockers.append('the output file is empty: the launch threw before returning a result (its error is in the Workflow tool result)')
    else:
        summary['launch'] = {'agents': output.get('agentCount'), 'tokens': output.get('totalTokens'), 'secs': elapsed(output.get('workflowProgress'))}
        summary['logs'] = logs(output.get('logs'))
        if not isinstance(result, dict):
            blockers.append('the output carries no result object')
    if isinstance(result, dict):
        summary['result'] = {k: v for k, v in result.items() if k not in CONDENSED}
        if result.get('state') and result.get('stateDigest'):
            summary['stateRef'] = {'outputFile': output_path, 'digest': result['stateDigest']}
        obs = result.get('observation') or {}
        reviews, ci, actions = obs.get('reviews') or {}, obs.get('ci'), obs.get('actions') or {}
        summary['observation'] = {
            'lane': obs.get('lane'), 'reviewedHead': obs.get('reviewedHead'),
            'bots': [{'bot': b.get('bot'), 'state': b.get('state'), 'sha': (b.get('sha') or '')[:8] or None} for b in reviews.get('bots') or []],
            'findings': [{'id': f.get('findingId'), 'digest': f.get('commentDigest'), 'source': f.get('source'), 'verdict': f.get('verdict'),
                          'at': f'{f.get("file")}:{f.get("line")}'} for f in reviews.get('findings') or []],
            'ci': ci and ci_summary(ci),
        }
        summary['receipts'], blocking = receipts(actions)
        blockers += blocking
        if actions.get('error'):
            blockers.append(f'action error: {cut(actions["error"])}')
        for f in (ci or {}).get('realFailures') or []:
            if f.get('complete') is not True:
                blockers.append(f'CI evidence incomplete for {f.get("check")} / {f.get("cell")}')
        # reviewedHead is where the last cycle began; a push it made moves the state past it.
        heads = {k: v for k, v in (('result', result.get('head')), ('state', (result.get('state') or {}).get('expectedHead')),
                                   ('checkout', tree and tree['head'])) if v}
        if len(set(heads.values())) > 1:
            blockers.append(f'heads disagree: {heads}')
    if tree and tree['dirty']:
        blockers.append(f'the checkout is dirty: {tree["dirty"][:10]}')
    if summary['stateRef'] is None and state_ref:
        summary['stateRef'] = state_ref
        notes.append('this launch returned no new state: continue from the stateRef it was given')
    return summary


def collect(argv):
    p = Parser(prog='launch_result.py', add_help=False)
    p.add_argument('--output', required=True)
    p.add_argument('--state-ref')
    p.add_argument('--checkout')
    a = p.parse_args(argv)
    ref = None
    if a.state_ref:
        file, sep, digest = a.state_ref.rpartition(':')
        if not sep or not file or not re.fullmatch(r'[0-9a-f]{8}', digest):
            raise Unusable(f'--state-ref must be FILE:DIGEST with an 8-hex digest, not {a.state_ref!r}')
        ref = {'outputFile': file, 'digest': digest}
    return summarize(load_output(a.output), str(Path(a.output).resolve()), ref, checkout(a.checkout) if a.checkout else None)


if __name__ == '__main__':
    sys.exit(report(collect, sys.argv[1:]))
