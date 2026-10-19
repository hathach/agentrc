"""pytest only: `--shard i/k` runs every k-th test by node id, so k CI jobs split the suite between them."""
import pytest


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
