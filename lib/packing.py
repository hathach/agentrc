"""Reading back a pr-babysit result whose arrays its workflow packed for the VM boundary; kept apart from seal.py, which claims AGENTRC_EMIT on import."""

# workflows/pr-babysit.js's CHUNKS: a launch's result saves an array its runtime's VM boundary cannot carry as
# {CHUNKS: its runs}.
CHUNKS = '#chunks'


def unpacked(v):
    """The workflow's unpacked: a saved result as the launch built it, each {CHUNKS: runs} its runs joined back into one
    array."""
    if isinstance(v, list):
        return [unpacked(x) for x in v]
    if not isinstance(v, dict):
        return v
    runs = unpacked(v[CHUNKS]) if list(v) == [CHUNKS] else None
    if isinstance(runs, list) and all(isinstance(r, list) for r in runs):
        return [x for r in runs for x in r]
    return {k: unpacked(x) for k, x in v.items()}
