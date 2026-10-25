# SystemView decoder corpus

`sysview_corpus.py` checks the native decoder (`skills/target-debug/scripts/sysview_decode.py`) against SystemView's
own exports of real recordings. `corpus.json` lists every file of the corpus with its SHA-256 and how each capture is
compared; `tests/fixtures/sysview/README.md` explains the comparison and its classes.

The corpus is too large for git (236 MB): it is the `sysview-corpus-v1.tar.xz` asset of this repository's `test-data`
release, which `corpus.json`'s `archive` pins. Its own `README.md` describes the layout and how a GUI export is
reproduced. The `test-data` release collects such datasets as `<skill>-<dataset>-v<N>.tar.xz`; an asset is never
replaced, a changed dataset is the next version.

```bash
mkdir /tmp/sysview-corpus &&
gh release download test-data -R hathach/agentrc -p sysview-corpus-v1.tar.xz -D /tmp/sysview-corpus &&
python3 -c "import json; a = json.load(open('bench/sysview/corpus.json'))['archive']; print(a['sha256'], a['asset'])" |
  (cd /tmp/sysview-corpus && sha256sum -c) &&
tar -xJf /tmp/sysview-corpus/sysview-corpus-v1.tar.xz -C /tmp/sysview-corpus &&
python bench/sysview/sysview_corpus.py --root /tmp/sysview-corpus/sysview-corpus-v1 \
  --manifest bench/sysview/corpus.json --out /tmp/sysview-corpus-diffs
```
