#!/usr/bin/env python3
"""One pr-babysit launch, condensed for its caller from the launch's saved Workflow output.

    launch_result.py --output FILE [--state-ref FILE:DIGEST] [--checkout DIR] [--keys]

--output is the launch's Workflow output file. It is empty when the launch threw
before returning, and then --state-ref, the stateRef that launch was given, is
the one to continue from. --checkout adds the checkout's branch, HEAD and dirty
paths. A CI failure that needs attention carries the `key` a caller passes back
as acceptedFailures: [{ key, reason, scope }]; a settled one (rig-side or accepted,
complete) is listed by its cell per check, and --keys adds each one's key.

`result` keeps every top-level result field verbatim except history, observation
and state, which are condensed; receipts are kept verbatim, each reply receipt
with its `batch` and `findingVerdicts`, the verdicts of this harvest's findings on
its comment (a refutedPosts reply answers stale findings as well as invalid ones),
and `botAsks` the trigger scripts' comments asking a bot to review (`already`
found one standing, `uncertain` posted one that did not read back).
`budget` is the returned state's
cyclesUsed and maxCycles; `pinned` its config, the arguments a continuation
passes unchanged, read from the --state-ref output when the launch returned no state. `blockers` lists what the caller must settle before
trusting or continuing the launch, a spent cycle budget and a refused launch
included, the latter with the caller's response, and a stop another launch on the same state would repeat,
with what must change first; `notes` what is absent but harmless.
A launch that stopped on a merged or closed PR is terminal: a note says so, and a
spent budget is no blocker.
`logs` leaves out lines another field carries whole and folds repeated ones into one.
"""
import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from facts import Parser, Unusable, git, report  # noqa: E402
from state_transfer import MAX  # noqa: E402

CONDENSED = ('history', 'observation', 'state')
NOT_FIXING = re.compile(r'^cycle \d+: [\w -]+ CI failure \(not fixing\): ')
# Whole in result.handoffs (commentId, replyId, the error in `why`) and result.settlements, when the record is there.
HANDED = (re.compile(r'^cycle \d+: reply \d+ to comment (?P<c>\d+) exists with the wrong content \(.*\) — handed to the caller, not another reply$', re.S),
          re.compile(r'^cycle \d+: no reply posted to comment (?P<c>\d+) \(.*\) — handed to the caller, who checks the thread for an earlier attempt$', re.S))
SETTLED = re.compile(r"^cycle \d+: comment (?P<c>\d+) settled on the caller's reply (?P<r>\d+)$")
COLLAPSED = (re.compile(r'^(?P<pre>cycle \d+: accepted failure )(?P<value>\S+)(?P<post> matches no failure on this head)$'),
             re.compile(r'^(?P<pre>accepted failure not renewed by this launch, no longer accepted: key )(?P<value>\S+)(?P<post>)$'))
SUMMARY = re.compile(r'^cycle \d+ summary — ')
IDE_DRIFT = re.compile(r'(?:.*/)?\.idea/')
CUT = 300
ADOPTION_REFUSED = 'the adoption was refused: investigate and report it, never answer it with a reset or a fabricated state'
BASE_FETCH = ('; this one lacks the base: fetch the base branch from the PR\'s base repository (not assuming the push remote, '
              'changing no credentials), then retry the same adoption with the same stateRef and adoptHead; '
              'a base that moved again can need another retry')
REFUSED = {
    'state-transfer-failed': 'the state did not load: keep the output file it came from and report it, never reset or reconstruct the state',
    'state-mismatch': "the PR or its remote differs from the state's pin (compare the result's pin with expected): report it; "
                      "starting over is the user's decision, never a reset or a fresh launch",
    'adopt-head-mismatch': ADOPTION_REFUSED, 'adopt-pending': ADOPTION_REFUSED, 'adopt-audit-failed': ADOPTION_REFUSED,
    'adopt-base-missing': ADOPTION_REFUSED + BASE_FETCH,
    'rebase-refused': 'the re-pin was refused: check out the rebasedHead or resolve the pending candidate, else report it; '
                      'never reset the state',
    'stale-head': "HEAD moved off the state's head since the last launch: commits on top of it rejoin by adoptHead; "
                  'a rewritten history (rebase, force-push) rejoins by rebasedHead naming the PR head, only on the user\'s word; '
                  'report anything else, never reset the state',
    'wrong-head': 'the checkout HEAD is not the PR head: check out the PR head, or name the local chain that holds it '
                  'as adoptHead; report anything else, never reset the state',
    'deferral-refused': 'a deferral was refused: it needs a new decision before it is passed again',
    'pr-conflicting': 'the PR conflicts with its base, so passing checks establish nothing: resolve the conflict in a merge from the base, '
                      "then relaunch with adoptHead naming that merge; rebasedHead is only for a rewrite on the user's word",
    'stale-workflow': "this session's cached workflow definition calls a script the install lacks: relaunch from a fresh session, never by hand",
    'pr-state-unread': 'the PR state could not be read before a GitHub write, so that write did not run: relaunch once `gh pr view` answers',
}
USER_ASKS = ('the user comments `@coderabbitai review` on the PR or accepts the head '
             'unreviewed, and a relaunch helps only once CodeRabbit reviewed the head, or on a new head')
CR_UNREVIEWED = 'CodeRabbit has not reviewed the head, and a coderabbitTrigger launch made its review required for every head of this run'
ENDED = {'pr-merged': 'the PR is merged; nothing to continue', 'pr-closed': 'the PR is closed; nothing to continue'}
# Stops another launch on the same state repeats: what must change first (chief's rule against relaunching on an unchanged blocker).
STALLED = {
    'ci-red-rig-side': 'CI is red only from rig-side failures: relaunch once they are repaired and re-run, or the user accepts them',
    'ci-red-sonar-gate': "the SonarCloud gate fails: relaunch only when markSonar has an answered issue to mark or the gate changed; "
                         "otherwise it is the user's to resolve or accept",
    'ci-red-greptile-gate': "Greptile's confidence check fails: a score below the repository's threshold needs a new Greptile review "
                            "of the head, else the user accepts it by its key; a check that could not be read as one (complete false) "
                            "needs inspecting first",
    'greptile-unreviewed': 'Greptile has not reviewed the head, and a greptileTrigger launch made its review required for every head of '
                           'this run: its request failed, was uncertain (the detail names the attempt record) or ambiguous, or no '
                           'review came within the cap after it; read `bots` and the PR, and relaunch once Greptile reviewed the head; '
                           "shipping the head unreviewed is the user's decision",
    'coderabbit-unreviewed': CR_UNREVIEWED + '; this output does not record its asks, and a failed tick leaves nothing on the PR, '
                             'so a relaunch may not ask again: ' + USER_ASKS,
    'ci-red-unclassified': 'a CI failure is unplaced: investigate it and pass the evidence as ciNotes before relaunching',
    'ci-stuck': 'the same checks stayed pending through two whole CI waits on this head, or a re-run named in `unregistered` never '
                'registered within ciWait: observe them once (a hung job, a missing runner) before relaunching',
    'github-rate-limited': 'GitHub refused a call for a rate limit (`rateLimit`): relaunch from the stateRef after its resetAt, '
                           'or after a few minutes for a secondary limit',
    'unactionable': 'the launch found nothing to act on: relaunch only on a named change (a push, a review, a CI result)',
    'harvest-failed': "harvest.py failed twice in a row (`detail`): fix what its error names, such as a module the "
                      'python3 on PATH lacks, or relaunch once a passing cause (a head still moving, a GitHub outage) cleared',
    'ci-collect-failed': "collect.py failed the same way on two cycles running (`detail`): fix what its error names, such as an "
                         'expired gh login or a module the python3 on PATH lacks, or relaunch once a passing cause (a GitHub outage) cleared',
    'reviews-stuck': "a review bot's state stayed unreadable, unchanged, for the wait cap: read its evidence in `bots` "
                     '(an artifact harvest.py does not know, a tie, a failed read) and relaunch once it changed or harvest.py reads it',
}


ASKS = {'tick': 'a Trigger review tick', 'request': 'an `@coderabbitai review` comment'}


def relaunch_ask(ask):
    """Whether a relaunch asks CodeRabbit on the head, from the run's own record of its asks (#243: a relaunch for an ask
    already spent)."""
    if ask.get('relaunchAsks') == 'tick':
        return 'it skipped the head for a manual review and this run never ticked its box on it: a relaunch with coderabbitTrigger ticks it once'
    if ask.get('relaunchAsks') == 'request':
        return (f"it is rate-limited until {ask.get('retryAt')} and this run never asked on the head: a relaunch with "
                'coderabbitTrigger after that comments `@coderabbitai review` once')
    asked = '; '.join(f"{ASKS.get(a.get('ask'), a.get('ask'))} {'at ' + a['at'] if a.get('at') else 'that failed'}"
                      for a in ask.get('asked') or [])
    why = f'this run already used its one ask on the head ({asked})' if asked else "nothing in CodeRabbit's state is ours to ask"
    return f'{why}, so a relaunch will not ask CodeRabbit on this head'


def coderabbit_blocker(ask):
    return f'{CR_UNREVIEWED}; {relaunch_ask(ask)}' + ('' if ask.get('relaunchAsks') in ASKS else f': {USER_ASKS}')


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


def finding_verdicts(findings, comment_id):
    """The verdicts of the findings harvested on one comment."""
    return sorted({f.get('verdict') for f in findings if f.get('commentId') == comment_id})


def receipts(cycles, findings):
    """Pushes, replies, bot asks and the adoption every cycle of the launch recorded, verbatim, and what about them blocks.

    A reply batch's `pass` is the workflow's own verdict that every comment it
    owed is settled, its detail naming any that is not; the receipts are evidence.
    A later batch of the same kind supersedes an earlier one's verdict, since it
    retries what that one left unsettled. Each receipt carries the verdicts its
    own cycle harvested, the last harvest where a cycle recorded none.
    """
    pushes, replies, asks, blocking, adoption, batches = [], [], [], [], None, {}
    for actions in cycles:
        asks += actions.get('botAsks') or []
        harvest = actions.get('findings', findings)
        for lane in ('reviewPush', 'ciPush'):
            p = actions.get(lane)
            if p:
                pushes.append({'lane': lane, **p})
                if p.get('pass') is not True or p.get('committed') is None:
                    blocking.append(f'uncertain publication: {lane}: {cut(p.get("detail", ""))}')
        for kind in ('refutedPosts', 'fixNotePosts', 'deferralPosts'):
            posts = actions.get(kind)
            if posts:
                replies += [{'batch': kind, 'findingVerdicts': finding_verdicts(harvest, r.get('commentId')), **r} for r in posts.get('receipts') or []]
                batches[kind] = posts
        adoption = actions.get('adoption') or adoption
    blocking += [f'reply batch did not pass: {kind}: {cut(posts.get("detail", ""))}'
                 for kind, posts in batches.items() if posts.get('pass') is not True]
    if adoption and adoption.get('publication') not in ('pushed', 'already-published', None):
        blocking.append(f'uncertain publication: adoption {adoption.get("from", "")[:8]}..{adoption.get("to", "")[:8]}: {adoption.get("publication")}')
    return {'pushes': pushes, 'replies': replies, 'botAsks': asks, 'adoption': adoption}, blocking


ATTENTION = ('check', 'cell', 'state', 'verdict', 'key', 'complete', 'files')


def ci_summary(ci, keys=False):
    """Counts by verdict, accepted and the gates apart; complete rig-side or accepted failures as their cells per check, or
    with keys as {cell, key}; every other failure by the fields a caller acts on, its firstError cut."""
    failures = ci.get('realFailures') or []
    settled = lambda f: f.get('complete') is True and f.get('state') in ('accepted', 'rigSide')
    cells = {}
    for f in filter(settled, failures):
        cells.setdefault(f.get('check'), []).append({'cell': f.get('cell'), 'key': f.get('key')} if keys else f.get('cell'))
    return {'status': ci.get('status'), 'headSha': ci.get('headSha'), 'infraRerun': ci.get('infraRerun'),
            'verdicts': dict(Counter(f.get('verdict') if f.get('state') in ('rigSide', 'real', 'unclassified') else f.get('state') for f in failures)),
            'settledCells': cells,
            'attention': [{**{k: f.get(k) for k in ATTENTION}, 'firstError': cut(f.get('firstError', ''))}
                          for f in failures if not settled(f)]}


def carried(line, result):
    """Whether the result holds the handoff or settlement record this log line reports."""
    result = result or {}
    m = next(filter(None, (p.match(line) for p in HANDED)), None)
    if m:
        return any(str(h.get('commentId')) == m['c'] for h in result.get('handoffs') or [])
    m = SETTLED.match(line)
    return bool(m) and any(str(st.get('commentId')) == m['c'] and str(st.get('replyId')) == m['r'] and st.get('outcome') == 'settled'
                           for st in result.get('settlements') or [])


def logs(lines, result=None):
    """Every log line cut to CUT, except: "not fixing" CI lines are counted, as ci lists those failures; a line whose record
    the result carries is dropped;
    a cycle summary keeps its header line, its table carried by observation; and the lines of one COLLAPSED pattern become one."""
    kept, counted, groups = [], 0, {}
    for l in lines or []:
        m = next(filter(None, (p.match(l) for p in COLLAPSED)), None)
        if NOT_FIXING.match(l):
            counted += 1
        elif carried(l, result):
            pass
        elif m:
            key = (m['pre'], m['post'])
            if key not in groups:
                groups[key] = []
                kept.append(key)
            groups[key].append(m['value'])
        else:
            head, _, table = l.partition('\n') if SUMMARY.match(l) else (l, '', '')
            l = f'{head} … ({table.count(chr(10)) + 1} more lines in the output\'s logs)' if table else l
            kept.append(l if len(l) <= CUT else f'{l[:CUT]}… ({len(l)} chars; whole line in the output\'s logs)')
    kept = [k if isinstance(k, str) else collapsed(*k, groups[k]) for k in kept]
    return kept + ([f'({counted} "not fixing" CI failure lines, one per failure listed under ci)'] if counted else [])


def collapsed(pre, post, values):
    """One line for a pattern's lines: its distinct values in first-seen order, and how many lines there were."""
    if len(values) == 1:
        return f'{pre}{values[0]}{post}'
    return f'{pre}{{{", ".join(dict.fromkeys(values))}}}{post} ({len(values)} lines)'


def ide_drift(line):
    """The workflow's IDE_DRIFT rule over a `git status --porcelain` line: every path it names lies under an .idea/ directory."""
    return all(IDE_DRIFT.match(p) for p in line[3:].split(' -> '))


def pinned_by(ref, notes):
    """The config of the state a stateRef names, or None with a note saying why."""
    try:
        result = ((load_output(ref['outputFile']) or {}).get('result') or {})
    except Unusable as e:
        notes.append(f'the stateRef\'s pinned arguments are unread: {e}')
        return None
    config = (result.get('state') or {}).get('config')
    if result.get('stateDigest') != ref['digest'] or not isinstance(config, dict):
        notes.append(f"the stateRef's output holds no state of digest {ref['digest']}: its pinned arguments are unread")
        return None
    return config


def checkout(path):
    return {'branch': git('-C', path, 'rev-parse', '--abbrev-ref', 'HEAD').strip(),
            'head': git('-C', path, 'rev-parse', 'HEAD').strip(),
            'dirty': [l for l in git('-C', path, 'status', '--porcelain').splitlines() if l]}


def summarize(output, output_path, state_ref=None, tree=None, keys=False):
    blockers, notes = [], []
    summary = {'launch': None, 'result': None, 'stateRef': None, 'budget': None, 'pinned': None, 'observation': None, 'receipts': None,
               'logs': [], 'checkout': tree, 'blockers': blockers, 'notes': notes}
    result = output.get('result') if output else None
    if output is None:
        blockers.append('the output file is empty: the launch threw before returning a result (its error is in the Workflow tool result)')
    else:
        summary['launch'] = {'agents': output.get('agentCount'), 'tokens': output.get('totalTokens'), 'secs': elapsed(output.get('workflowProgress'))}
        summary['logs'] = logs(output.get('logs'), output.get('result'))
        if not isinstance(result, dict):
            blockers.append('the output carries no result object')
    if isinstance(result, dict):
        summary['result'] = {k: v for k, v in result.items() if k not in CONDENSED}
        if result.get('state') and result.get('stateDigest'):
            summary['stateRef'] = {'outputFile': output_path, 'digest': result['stateDigest']}
            used, most = result['state'].get('cyclesUsed'), result['state'].get('maxCycles')
            summary['budget'] = {'cyclesUsed': used, 'maxCycles': most}
            summary['pinned'] = result['state'].get('config')
            if result.get('pass') is not True and result.get('reason') not in ENDED and isinstance(used, int) and isinstance(most, int) and used >= most:
                blockers.append(f'cycle budget spent: {used} of {most}; a relaunch needs a larger maxCycles, the user\'s decision')
        if result.get('stateTooLarge'):
            blockers.append(f"the returned state is {result['stateTooLarge']} bytes, over the {MAX} a launch loads: no launch can continue "
                            'from it; keep the output file and report it, never reset or reconstruct the state')
        if result.get('reason') in REFUSED:
            blockers.append(REFUSED[result['reason']])
        if result.get('reason') in ENDED:
            notes.append(ENDED[result['reason']])
        ask = result.get('coderabbitAsk') if isinstance(result.get('coderabbitAsk'), dict) else None
        if ask and result.get('reason') == 'coderabbit-unreviewed':
            blockers.append(coderabbit_blocker(ask))
        else:
            if result.get('reason') in STALLED:
                blockers.append(STALLED[result['reason']])
            if ask:
                notes.append(f'CodeRabbit has not reviewed the head; {relaunch_ask(ask)}')
        if result.get('reason') == 'held-awaiting-caller':
            blockers += [f"finding {h.get('findingId')} on comment {h.get('commentId')} is held for the caller, and another launch "
                         f"cannot lift it: {cut(h.get('reason') or '')}" for h in result.get('held') or []]
        obs = result.get('observation') or {}
        reviews, ci, actions = obs.get('reviews') or {}, obs.get('ci'), obs.get('actions') or {}
        summary['observation'] = {
            'lane': obs.get('lane'), 'reviewedHead': obs.get('reviewedHead'),
            'bots': [{'bot': b.get('bot'), 'state': b.get('state'), 'kind': b.get('kind'), 'sha': (b.get('sha') or '')[:8] or None,
                      'reason': cut(b.get('reason') or ''), 'evidence': [cut(e) for e in b.get('evidence') or []], 'retryAt': b.get('retryAt')}
                     for b in reviews.get('bots') or []],
            'findings': [{'id': f.get('findingId'), 'digest': f.get('commentDigest'), 'source': f.get('source'), 'verdict': f.get('verdict'),
                          'at': f'{f.get("file")}:{f.get("line")}', **({'held': cut(f['hold'])} if f.get('hold') else {})}
                         for f in reviews.get('findings') or []],
            'ci': ci and ci_summary(ci, keys),
        }
        # A launch that ran several cycles lists each cycle's actions; an older output has the last one only.
        summary['receipts'], blocking = receipts(obs.get('launchActions') or [actions], reviews.get('findings') or [])
        blockers += blocking
        if actions.get('error'):
            blockers.append(f'action error: {cut(actions["error"])}')
        unsettled = {st.get('commentId'): f"settlement {st.get('outcome')}: {cut(st.get('why') or '')}"
                     for st in result.get('settlements') or [] if st.get('outcome') != 'settled'}
        for h in result.get('handoffs') or []:
            blockers.append(f"comment {h.get('commentId')} is the caller's to answer (chief's reply takeover rule), then to settle through replySettlements: {cut(h.get('why') or '')}"
                            + (f"; draft: {cut(h['draft'])}" if h.get('draft') else '')
                            + (f"; the user's rejudge request: {cut(h['rejudge'].get('reason') or '')}" if isinstance(h.get('rejudge'), dict) else '')
                            + (f"; {unsettled.pop(h.get('commentId'))}" if h.get('commentId') in unsettled else ''))
        blockers += [f'comment {c}: {why}' for c, why in unsettled.items()]
        if result.get('unreviewed'):
            notes.append('passed with reviewer(s) that did not review the head: ' + '; '.join(
                f"{b.get('bot')} {b.get('state')}{'/' + b['kind'] if b.get('kind') else ''} ({cut(b.get('reason') or '')})" for b in result['unreviewed']))
        for u in result.get('sonarUnmarked') or []:
            last = u.get('last') or {}
            blockers.append(f"SonarCloud issue of comment {u.get('commentId')} not marked false positive: "
                            f"{last.get('outcome', 'not asked')}{': ' + cut(last['detail']) if last.get('detail') else ''}")
        for f in (ci or {}).get('realFailures') or []:
            if f.get('complete') is not True:
                blockers.append(f'CI evidence incomplete for {f.get("check")} / {f.get("cell")}')
        # reviewedHead is where the last cycle began; a push it made moves the state past it.
        heads = {k: v for k, v in (('result', result.get('head')), ('state', (result.get('state') or {}).get('expectedHead')),
                                   ('checkout', tree and tree['head'])) if v}
        if len(set(heads.values())) > 1:
            blockers.append(f'heads disagree: {heads}')
    dirty = [l for l in (tree or {}).get('dirty') or [] if not ide_drift(l)]
    if tree and len(dirty) < len(tree['dirty']):
        notes.append(f'ignoring {len(tree["dirty"]) - len(dirty)} dirty .idea/ path(s) (IDE metadata)')
    if dirty:
        blockers.append(f'the checkout is dirty: {dirty[:10]}')
    if summary['stateRef'] is None and state_ref:
        summary['stateRef'] = state_ref
        notes.append('this launch returned no new state: continue from the stateRef it was given')
        summary['pinned'] = pinned_by(state_ref, notes)
    return summary


def collect(argv):
    p = Parser(prog='launch_result.py', description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--output', required=True)
    p.add_argument('--state-ref')
    p.add_argument('--checkout')
    p.add_argument('--keys', action='store_true')
    a = p.parse_args(argv)
    ref = None
    if a.state_ref:
        file, sep, digest = a.state_ref.rpartition(':')
        if not sep or not file or not re.fullmatch(r'[0-9a-f]{8}', digest):
            raise Unusable(f'--state-ref must be FILE:DIGEST with an 8-hex digest, not {a.state_ref!r}')
        ref = {'outputFile': file, 'digest': digest}
    return summarize(load_output(a.output), Path(a.output).resolve().as_posix(), ref, checkout(a.checkout) if a.checkout else None, a.keys)


if __name__ == '__main__':
    sys.exit(report(collect, sys.argv[1:]))
