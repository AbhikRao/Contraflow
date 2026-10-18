"""Copy a consistent frontend snapshot without changing the active designer checkout.
Run again with --final only once the user/designer confirms completion.
"""
import argparse
import hashlib
import json
import shutil
import subprocess
import time
from pathlib import Path

p = argparse.ArgumentParser()
p.add_argument("source", type=Path)
p.add_argument("--final", action="store_true")
a = p.parse_args()
root = Path(__file__).resolve().parents[1]
source = a.source.resolve()
assert source != root
names = subprocess.check_output(["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z", "--", "web"], cwd=source).decode().split("\0")
files = sorted({n for n in names if n and (source / n).is_file() and ".env" not in Path(n).name})
def hashes():
    return {n: hashlib.sha256((source / n).read_bytes()).hexdigest() for n in files}
before = hashes()
for n in files:
    dest = root / n
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source / n, dest)
assert hashes() == before, "Designer files changed during staging; rerun after they settle"
manifest = {"source_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=source, text=True).strip(),
            "frontend_sha256": hashlib.sha256(json.dumps(before, sort_keys=True).encode()).hexdigest(),
            "files": before, "final_ui_confirmed": a.final, "staged_at_epoch": time.time()}
(root / "deploy_public" / "release.json").write_text(json.dumps(manifest, indent=2) + "\n")
print(json.dumps({k: v for k, v in manifest.items() if k != "files"}, indent=2))
