#!/usr/bin/env python3
"""Read the Docs by build URL, which is all a GitHub check's details URL carries.

  rtd.py rerun <build-url>...
  rtd.py log <build-url>

A build URL is https://app.readthedocs.org/projects/<project>/builds/<id>/ (or
app.readthedocs.com), the target of a failed `docs/readthedocs.org:<project>`
status; the API is that host's /api/v3.
rerun: triggers a new build of each build's version (a PR build's version is
the PR number, which the versions listing hides but the trigger accepts);
stdout ends with one JSON line {"reruns": [{"build", "version", "newBuild"}],
"errors": [...]}. Exit 0 when every build was re-run, 1 otherwise, 2 on a
usage error or a missing token.
log: prints the build's state, its error, its notifications and the tail of
each failed command (API v2), the failure reason where the web log may be
rate-limited.

The token is RTD_TOKEN from the environment, else from a login shell.
"""

import argparse
import html
import http.client
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request

BUILD = re.compile(r'^(https://app\.readthedocs\.(?:org|com))/projects/([\w-]+)/builds/(\d+)/?$')


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


# urllib carries the Authorization header across a redirect to any host.
OPENER = urllib.request.build_opener(NoRedirect)


class Failed(Exception):
    pass


def token():
    value = os.environ.get('RTD_TOKEN')
    if not value:
        try:
            value = subprocess.run(['bash', '-lc', 'printf %s "$RTD_TOKEN"'],
                                   capture_output=True, text=True, timeout=10).stdout.strip()
        except (OSError, subprocess.SubprocessError) as e:
            raise Failed(f'RTD_TOKEN is not in the environment and the login shell failed: {e}')
    if not value:
        raise Failed('RTD_TOKEN is not set, in the environment or a login shell')
    # Never echo it: anything the profile printed would ride along into the header and its error.
    if not re.fullmatch(r'[A-Za-z0-9_.-]+', value):
        raise Failed('RTD_TOKEN is not a single token (does the login shell print anything?)')
    return value


def call(url, key, method='GET'):
    request = urllib.request.Request(url, method=method, headers={'Authorization': f'Token {key}'})
    try:
        with OPENER.open(request, timeout=60) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        raise Failed(f'{method} {url}: HTTP {e.code} {e.read().decode(errors="replace")[:200]}')
    except (OSError, ValueError, http.client.HTTPException) as e:
        raise Failed(f'{method} {url}: {e}')


def parse(url):
    m = BUILD.match(url)
    if not m:
        raise Failed(f'{url}: not a build URL (https://app.readthedocs.org/projects/<project>/builds/<id>/)')
    host, project, build = m.groups()
    return host, f'{host}/api/v3/projects/{project}', int(build)


def rerun(urls, key):
    reruns, errors, triggered = [], [], {}
    for url in urls:
        try:
            _, api, build = parse(url)
            version = call(f'{api}/builds/{build}/', key).get('version')
            if not isinstance(version, str) or not version:
                raise Failed(f'build {build}: no version in its record ({version!r})')
            if (api, version) not in triggered:
                # Marked before the POST: a lost answer may still have started a build.
                triggered[api, version] = None
                new = (call(f'{api}/versions/{version}/builds/', key, method='POST').get('build') or {}).get('id')
                if not isinstance(new, int):
                    raise Failed(f'build {build}: the trigger answered without a new build id')
                triggered[api, version] = new
            new = triggered[api, version]
            if new is None:
                raise Failed(f'build {build}: version {version} was already triggered in this run without a confirmed build')
            reruns.append({'build': build, 'version': version, 'newBuild': new})
        except Failed as e:
            errors.append(str(e))
    print(json.dumps({'reruns': reruns, 'errors': errors}))
    return 0 if not errors else 1


def reason(url, key):
    """The build's record, its notifications and each failed command's last 40 output lines."""
    host, api, build = parse(url)
    record = call(f'{api}/builds/{build}/', key)
    notes = [(m.get('type'), m.get('header'), html.unescape(re.sub(r'<[^>]+>', '', m.get('body') or '')))
             for m in (n.get('message') or {} for n in call(f'{api}/builds/{build}/notifications/', key).get('results', []))]
    failed = [(c['exit_code'], c.get('command'), '\n'.join((c.get('output') or '').splitlines()[-40:]))
              for c in call(f'{host}/api/v2/build/{build}/', key).get('commands', []) if c.get('exit_code')]
    return build, record, notes, failed


def log(url, key):
    build, record, notes, failed = reason(url, key)
    state = (record.get('state') or {}).get('code')
    print(f'build {build}: {state}, success {record.get("success")}, {record.get("duration")} s, commit {record.get("commit")}')
    if record.get('error'):
        print(f'error: {record["error"]}')
    for kind, header, body in notes:
        print(f'== {kind}: {header}\n{body}')
    for code, command, tail in failed:
        print(f'== command exited {code}: {command}\n{tail}')
    if not notes and not failed and not record.get('error') and record.get('success') is False:
        print('no failure reason recorded by the API')
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('command', choices=['rerun', 'log'])
    p.add_argument('builds', nargs='+', metavar='build-url')
    a = p.parse_args(argv)
    if a.command == 'log' and len(a.builds) != 1:
        p.error('log takes one build URL')
    try:
        key = token()
    except Failed as e:
        print(f'rtd.py: {e}', file=sys.stderr)
        return 2
    try:
        return rerun(a.builds, key) if a.command == 'rerun' else log(a.builds[0], key)
    except Failed as e:
        print(f'rtd.py: {e}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
