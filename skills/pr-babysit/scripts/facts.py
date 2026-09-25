"""What pr-babysit's scripts share: the {"error"} line contract and strict reads."""

import argparse
import json
import os
import re
import subprocess

FULL_SHA = re.compile(r'^[0-9a-f]{40}$')


class Unusable(Exception):
    pass


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise Unusable(f'usage: {message}')


def run(*argv, ok=(0,)):
    """(exit status, stdout); a status outside ok, or stdout that is not UTF-8, is Unusable."""
    try:
        done = subprocess.run(argv, capture_output=True)
    except FileNotFoundError:
        raise Unusable(f'{argv[0]} is not installed')
    if done.returncode not in ok:
        raise Unusable(f"{' '.join(argv)}: {done.stderr.decode(errors='replace').strip()}")
    try:
        return done.returncode, done.stdout.decode()
    except UnicodeDecodeError:
        # Replacing the bad bytes could make two different paths read as one.
        raise Unusable(f"{' '.join(argv)}: output is not UTF-8")


def attempt(*argv, input=None):
    """(exit status, stdout, stderr), replacing bad bytes: unlike run, a failure is part of the receipt."""
    try:
        done = subprocess.run(argv, capture_output=True, text=True, errors='replace', input=input)
    except FileNotFoundError:
        raise Unusable(f'{argv[0]} is not installed')
    return done.returncode, done.stdout, done.stderr


def git(*argv):
    return run('git', *argv)[1]


def checkout_top():
    """The checkout's top level, Unusable unless it is the current directory."""
    top = git('rev-parse', '--show-toplevel').strip()
    if os.path.realpath(top) != os.path.realpath('.'):
        raise Unusable(f'run from the checkout top level {top}, not {os.getcwd()}')
    return top


def report(collect, argv):
    """Print collect(argv) as the last stdout line and return 0, or {"error"} and 2."""
    try:
        facts = collect(argv)
    except Unusable as e:
        print(json.dumps({'error': str(e)}))
        return 2
    print(json.dumps(facts))
    return 0
