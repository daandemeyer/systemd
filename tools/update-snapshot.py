#!/usr/bin/env python3
# SPDX-License-Identifier: LGPL-2.1-or-later

"""
Check out pkg/{distribution}.
With -u, fetch commits, and if changed, commit the latest hash.
"""

import argparse
import itertools
import shlex
import subprocess
from typing import Optional

from pathlib import Path

def parse_args():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--distribution', '-d', 'distribution')
    p.add_argument('--release', '-r', 'release', default=None)
    p.add_argument('--commit', '-c', action='store_true', default=False)

    return p.parse_args()


def commit_files(distribution: str, release: Optional[str], files: list[Path], snapshot: str):
    msg = f'mkosi: update {distribution} {release} snapshot to {snapshot}'
    cmd = ['git', 'commit', '-m', msg, *(str(file) for file in files)]
    print(f"+ {shlex.join(cmd)}")
    subprocess.check_call(cmd)


if __name__ == "__main__":
    args = parse_args()

    cmd = [
        'mkosi',
        '-d', args.distribution,
        '-r', args.release,
        'latest-snapshot',
    ]
    print(f"+ {shlex.join(cmd)}")
    snapshot = subprocess.check_output(cmd, text=True).strip()

    tocommit = []

    p = Path(f"mkosi/mkosi.conf.d/{args.distribution}/mkosi.conf.d/snapshot-{args.release}.conf")
    old = p.read_text().splitlines()
    new = [f"Snapshot={snapshot}" if line.startswith("Snapshot=") else line for line in old]
    if new != old:
        p.write_text("\n".join(new) + "\n")
        tocommit += [p]

    if args.commit and tocommit:
        commit_files(args.distribution, args.release, tocommit, snapshot)
