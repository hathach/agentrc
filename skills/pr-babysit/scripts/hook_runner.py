"""Run pre-commit's own main() and record what each hook did, for hooks.py to attribute output by hook.

  <pre-commit's python> hook_runner.py PATH0 RECORDS [--only ID] ARG...

Run by the interpreter the pre-commit entry script names (hooks.py's
pre_commit_python()), with sys.path[0] set to PATH0, what that entry script has
there, so pre-commit imports as it does. ARG... is pre-commit's command line;
stdout, stderr and the exit status are pre-commit's own. RECORDS, an existing
file, gets one JSON line per record, each flushed:
  {record: start, version, supported}, then, for a supported version only:
  {record: hooks, hooks: [{id, alias, stages}]}: every hook the config and
    manifests resolve to, at any stage, in config order;
  {record: roster, failFast, hooks: [{id, alias, failFast, passFilenames}]}: the
    hooks the run selected, in run order, after the config and manifests are
    resolved; failFast, the config's or --fail-fast;
  {record: hook, index, id, executed, retcode, modified, failed, output} per
    _run_single_hook() call, index its roster position;
  {record: end, code} once main() returns.
With --only ID the run goes on only when the roster is exactly hook ID: else
the roster record carries refused true and pre-commit exits 1 having run none.
Only observation: dispatch, filenames, environments, retries and fail_fast stay
pre-commit's. An error in the observation fails the run, and a record missing
its end is partial; hooks.py attributes nothing from either.
"""
import sys

# The entry script's own sys.path[0], never this directory, whose scripts could shadow what pre-commit imports.
sys.path[0] = sys.argv[1]

import inspect  # noqa: E402
import json  # noqa: E402

# Each version whose commands/run.py source was read: run() selects its hooks from all_hooks(config, store), a module
# global, whose Hooks carry id, alias and stages (repository.py, hook.py); _run_hooks(config, hooks, skips, args) calls
# _run_single_hook(classifier, hook, skips, cols, diff_before, ...) -> (failed, diff_after) by module global per hook
# in order, a hook's files modified when diff_after differs from diff_before, and
# languages[hook.language].run_hook(...) -> (retcode, out) once for each hook it executes.
SUPPORTED = ('4.2.0', '4.3.0', '4.4.0', '4.5.0', '4.5.1', '4.6.0', '4.6.1', '4.6.2', '4.7.0')


class Observed:
    """A language whose run_hook() result is kept in ran; everything else is the language's own."""

    def __init__(self, language, ran):
        self._language, self._ran = language, ran

    def __getattr__(self, name):
        return getattr(self._language, name)

    def run_hook(self, *args, **kwargs):
        result = self._language.run_hook(*args, **kwargs)
        self._ran.append(result)
        return result


def observe(write, only):
    from pre_commit.all_languages import languages
    from pre_commit.commands import run

    all_hooks, run_hooks, run_single_hook = run.all_hooks, run._run_hooks, run._run_single_hook
    roster, done = [], []

    def observed_all(config, store):
        found = all_hooks(config, store)
        write(record='hooks', hooks=[{'id': h.id, 'alias': h.alias, 'stages': list(h.stages)} for h in found])
        return found

    def observed_hooks(config, hooks, skips, args):
        roster[:] = hooks
        refused = only is not None and [h.id for h in hooks] != [only]
        write(record='roster', failFast=bool(config['fail_fast'] or getattr(args, 'fail_fast', False)),
              hooks=[{'id': h.id, 'alias': h.alias, 'failFast': bool(h.fail_fast), 'passFilenames': bool(h.pass_filenames)}
                     for h in hooks],
              **({'refused': True} if refused else {}))
        return 1 if refused else run_hooks(config, hooks, skips, args)

    def observed_single(classifier, hook, *args, **kwargs):
        index = len(done)
        if index >= len(roster) or roster[index] is not hook:
            raise RuntimeError(f'hook_runner.py: hook {hook.id!r} ran out of its roster order')
        diff_before = inspect.signature(run_single_hook).bind(classifier, hook, *args, **kwargs).arguments[
            'diff_before']
        ran, language = [], languages.get(hook.language)
        if language is not None:
            languages[hook.language] = Observed(language, ran)
        try:
            failed, diff_after = run_single_hook(classifier, hook, *args, **kwargs)
        finally:
            if language is not None:
                languages[hook.language] = language
        retcode, out = ran[0] if ran else (None, b'')
        done.append(hook)
        write(record='hook', index=index, id=hook.id, executed=bool(ran), retcode=retcode,
              modified=diff_before != diff_after, failed=bool(failed), output=out.decode('utf-8', 'replace'))
        return failed, diff_after

    run.all_hooks, run._run_hooks, run._run_single_hook = observed_all, observed_hooks, observed_single


def main(argv):
    records, rest = argv[1], argv[2:]
    only = None
    if rest[:1] == ['--only']:
        only, rest = rest[1], rest[2:]
    from pre_commit import constants
    from pre_commit.main import main as pre_commit_main

    with open(records, 'a', encoding='utf-8') as f:
        def write(**record):
            f.write(json.dumps(record) + '\n')
            f.flush()

        supported = constants.VERSION in SUPPORTED
        write(record='start', version=constants.VERSION, supported=supported)
        if supported:
            observe(write, only)
        code = pre_commit_main(rest)
        if supported:
            write(record='end', code=code)
    return code


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
