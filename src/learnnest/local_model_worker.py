"""Network-enabled child process used only by an explicit model install request."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from learnnest.local_models import package_spec


def _size(directory: Path) -> int:
    return sum(path.stat().st_size for path in directory.rglob("*") if path.is_file())


def install(package_id: str, staging: Path) -> None:
    from huggingface_hub import snapshot_download

    package = package_spec(package_id)
    for component in package.components:
        destination = staging / component.name
        snapshot_download(
            repo_id=component.repo_id,
            revision=component.revision,
            local_dir=destination,
            allow_patterns=list(component.required_files),
            max_workers=1,
        )
        print(
            json.dumps(
                {
                    "event": "progress",
                    "component": component.name,
                    "downloaded_bytes": _size(staging),
                }
            ),
            flush=True,
        )


def main() -> None:
    parser = argparse.ArgumentParser(prog="learnnest.local_model_worker")
    parser.add_argument("package_id")
    parser.add_argument("staging", type=Path)
    args = parser.parse_args()
    install(args.package_id, args.staging)


if __name__ == "__main__":
    main()
