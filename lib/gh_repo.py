"""The one GitHub repository a checkout's remotes name, for a script that calls gh without --repo: `gh repo view`
picks by its own rules, the wrong one in a fork of a fork (#311). Stdlib only.

A script reaches it through its resolved path, as it reaches lib/windows.py:
`sys.path.insert(0, str(Path(__file__).resolve().parents[N] / 'lib'))`, then `import gh_repo`."""
import re
import subprocess

HOST = 'github.com'
REPO = re.compile(r'\A[A-Za-z0-9][A-Za-z0-9-]*/(?!\.\.?\Z)[A-Za-z0-9._-]+\Z')  # OWNER/NAME
# scp-like needs `:` and a URL `/` after the host: accepting either makes `git@github.com/owner/repo` look right while git reads a local path.
ORIGIN = re.compile(r'(?:(?:https|ssh)://(?:[^@/]*@)?([^/:]+)(?::[0-9]+)?/|(?:[^@/\s]+@)([^/:]+):)([^/]+)/([^/]+?)(?:\.git)?')


class Unresolved(Exception):
    """No single repository: the message says why; the caller adds how to name one."""


def github_repo(url):
    """OWNER/NAME of a github.com remote URL as spelled there, '' for any other URL."""
    m = ORIGIN.fullmatch(url.strip().rstrip('/'))
    return f'{m[3]}/{m[4]}' if m and (m[1] or m[2]).lower() == HOST else ''


def checkout_repo(cwd=None):
    """The one GitHub repository the remotes of the checkout at cwd name: those `gh repo set-default` marked
    gh-resolved (`base` for the remote's own), else every remote URL; Unresolved when they name none or several."""
    try:
        done = subprocess.run(['git', 'config', '--get-regexp', r'^remote\..+\.(url|gh-resolved)$'],
                              capture_output=True, cwd=cwd)
    except FileNotFoundError:
        raise Unresolved('git is not installed') from None
    if done.returncode not in (0, 1):   # 1: no remote at all
        raise Unresolved(f"git config --get-regexp: {done.stderr.decode(errors='replace').strip()}")
    try:
        got = done.stdout.decode()
    except UnicodeDecodeError:
        raise Unresolved('git config --get-regexp: output is not UTF-8') from None
    urls, marked = {}, {}
    for line in got.splitlines():
        key, _, value = line.partition(' ')
        remote, _, field = key[len('remote.'):].rpartition('.')
        if field == 'url':
            urls.setdefault(remote, value.strip())  # git fetches from a remote's first URL
        else:
            marked[remote] = value.strip()  # listed system to worktree: the last is git's effective value
    named = ({r: github_repo(urls.get(r, '')) if v == 'base' else v for r, v in marked.items()} if marked
             else {r: n for r, u in urls.items() if (n := github_repo(u))})
    repos = {}
    for n in named.values():
        repos.setdefault(n.lower(), n)  # GitHub names ignore case: one repository, spelled as its first remote does
    if len(repos) == 1 and '' not in repos:
        name = next(iter(repos.values()))
        if REPO.match(name):
            return name
        raise Unresolved(f'gh-resolved names {name!r}, not a repository OWNER/NAME')
    which = ', '.join(f'{r} {n or "(not a GitHub repository)"}' for r, n in sorted(named.items()))
    raise Unresolved(f'the remotes marked gh-resolved name {which}' if marked
                     else f'the remotes name {len(repos)} GitHub repositories ({which}) and none is marked gh-resolved'
                     if repos else 'no remote names a github.com repository')
