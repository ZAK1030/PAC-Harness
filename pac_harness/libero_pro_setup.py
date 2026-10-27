"""Fetch pinned LIBERO-Pro task definitions and states, without demonstrations."""
from __future__ import annotations

import argparse
import ast
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import time
import urllib.parse
import urllib.request

from adapters.libero import PRO_SUITES
from .storage import write_json

SOURCE_REVISION = "eafdb809426b13153aa1e4c42d6601844217dfec"
DATASET_REPO = "zhouxueyang/LIBERO-Pro"
DATASET_REVISION = "c86fc3b8293185a6f373677018ff3e37f8391602"


def fetch(url):
    for attempt in range(3):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 PAC-Harness"})
            with urllib.request.urlopen(request, timeout=45) as response:
                return response.read()
        except OSError:
            if attempt == 2:
                raise
            time.sleep(attempt + 1)


def task_map(repo):
    path = Path(repo) / "libero/libero/benchmark/libero_suite_task_map.py"
    for node in ast.parse(path.read_text(encoding="utf-8")).body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "libero_task_map" for t in node.targets):
            return ast.literal_eval(node.value)
    raise ValueError(f"No task map found in {path}")


def download(repo, destination, endpoint):
    destination = Path(destination).resolve()
    endpoint = endpoint.rstrip("/")
    if not endpoint.startswith("https://"):
        raise ValueError("Dataset endpoint must use HTTPS")
    mapping = task_map(repo)
    directories = [f"{kind}/{suite}" for suite in PRO_SUITES for kind in ("bddl_files", "init_files")]

    def listing(directory):
        url = f"{endpoint}/api/datasets/{DATASET_REPO}/tree/{DATASET_REVISION}/{directory}?limit=100"
        return json.loads(fetch(url))

    with ThreadPoolExecutor(max_workers=6) as pool:
        listings = list(pool.map(listing, directories))
    entries = {entry["path"]: entry for group in listings for entry in group if entry["type"] == "file"}
    required = []
    for suite in PRO_SUITES:
        for name in mapping[suite]:
            for kind, suffix in (("bddl_files", ".bddl"), ("init_files", ".pruned_init")):
                relative = f"{kind}/{suite}/{name}{suffix}"
                required.append(entries[relative])

    def transfer(entry):
        relative = entry["path"]
        path = destination / relative

        def valid(raw):
            if len(raw) != entry["size"]:
                return False
            if "lfs" in entry:
                return hashlib.sha256(raw).hexdigest() == entry["lfs"]["oid"]
            blob = b"blob " + str(len(raw)).encode() + b"\0" + raw
            return hashlib.sha1(blob).hexdigest() == entry["oid"]

        raw = path.read_bytes() if path.exists() else b""
        if not valid(raw):
            url = f"{endpoint}/datasets/{DATASET_REPO}/resolve/{DATASET_REVISION}/{urllib.parse.quote(relative)}"
            raw = fetch(url)
            if not valid(raw):
                raise ValueError(f"Dataset size/hash mismatch: {relative}")
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix(path.suffix + ".partial")
            temporary.write_bytes(raw)
            temporary.replace(path)
        return {"path": relative, "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}

    with ThreadPoolExecutor(max_workers=6) as pool:
        records = list(pool.map(transfer, required))
    write_json(destination / "manifest.json", {"source_revision": SOURCE_REVISION,
        "dataset_repo": DATASET_REPO, "dataset_revision": DATASET_REVISION,
        "endpoint": endpoint, "suites": list(PRO_SUITES), "files": records})
    print(f"LIBERO-Pro: verified {len(records)} task/state files in {destination}", flush=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--data", required=True)
    parser.add_argument("--endpoint", default=os.environ.get("HF_ENDPOINT", "https://huggingface.co"))
    args = parser.parse_args(argv)
    download(args.repo, args.data, args.endpoint)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
