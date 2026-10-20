"""pytest only (unittest discover never loads this): the workers' environment, and `--shard i/k`, which runs every
k-th test by node id, so k CI jobs split the suite between them."""
import os

import pytest


def pytest_configure(config):
    # before xdist starts its workers, which inherit the environment but not the controller's -X utf8
    os.environ['PYTHONUTF8'] = '1'
    # git exports these to a pre-commit hook (GIT_INDEX_FILE relative to the committing checkout); a test's git
    # in its own temporary repository must not inherit them. Not every GIT_*: GIT_CONFIG_* carries README's
    # fsmonitor workaround
    for name in ('GIT_DIR', 'GIT_WORK_TREE', 'GIT_INDEX_FILE', 'GIT_COMMON_DIR', 'GIT_OBJECT_DIRECTORY',
                 'GIT_ALTERNATE_OBJECT_DIRECTORIES', 'GIT_PREFIX', 'GIT_NAMESPACE'):
        os.environ.pop(name, None)


def pytest_addoption(parser):
    parser.addoption('--shard', metavar='I/K', help='run the I-th of K disjoint parts of the suite (0-based)')


def pytest_collection_modifyitems(config, items):
    spec = config.getoption('--shard')
    if spec is None:
        return
    try:
        i, k = (int(n) for n in spec.split('/'))
    except ValueError:
        raise pytest.UsageError(f'--shard {spec}: expected I/K')
    if not 0 <= i < k:
        raise pytest.UsageError(f'--shard {spec}: I must be in 0..K-1')
    mine = set(sorted(item.nodeid for item in items)[i::k])
    config.hook.pytest_deselected(items=[item for item in items if item.nodeid not in mine])
    items[:] = [item for item in items if item.nodeid in mine]
