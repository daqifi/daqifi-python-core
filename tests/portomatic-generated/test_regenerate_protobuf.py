"""Verify ``scripts/regenerate_protobuf.py`` produces output matching what's
checked in (closes part of #8 — build step + drift detection).

If a contributor edits ``daqifi/_internal/proto/DaqifiOutMessage.proto``
without re-running the regen script, the checked-in ``*_pb2.py`` will
diverge from the .proto. This test catches that drift in CI.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPT = REPO_ROOT / "scripts" / "regenerate_protobuf.py"
PROTO_DIR = REPO_ROOT / "daqifi" / "_internal" / "proto"


def _have_grpcio_tools() -> bool:
    try:
        import grpc_tools  # noqa: F401
        return True
    except ImportError:
        return False


@pytest.mark.skipif(
    not _have_grpcio_tools(),
    reason="grpcio-tools is required to regenerate protobuf bindings",
)
def test_regenerate_script_emits_idempotent_output(tmp_path: Path) -> None:
    """Running the regen script over the current .proto must produce
    bytes identical to the checked-in ``*_pb2.py`` files."""
    # Copy .proto files to a sandbox so we don't touch the real tree.
    sandbox = tmp_path / "proto"
    sandbox.mkdir()
    for proto in PROTO_DIR.glob("*.proto"):
        shutil.copy(proto, sandbox / proto.name)

    rc = subprocess.run(
        [
            sys.executable,
            "-m", "grpc_tools.protoc",
            f"--proto_path={sandbox}",
            f"--python_out={sandbox}",
            *[p.name for p in sandbox.glob("*.proto")],
        ],
        check=False,
        capture_output=True,
    ).returncode
    assert rc == 0, "regen script failed"

    for pb2 in PROTO_DIR.glob("*_pb2.py"):
        regenerated = sandbox / pb2.name
        assert regenerated.exists(), f"regen did not produce {pb2.name}"
        # Compare byte-for-byte. If this fails after a .proto edit, run
        # ``python3 scripts/regenerate_protobuf.py`` and commit the diff.
        assert regenerated.read_bytes() == pb2.read_bytes(), (
            f"checked-in {pb2.relative_to(REPO_ROOT)} is out of sync with "
            f"{pb2.with_suffix('').name}.proto — run "
            "`python3 scripts/regenerate_protobuf.py` and commit the diff"
        )
