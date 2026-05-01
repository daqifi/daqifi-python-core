"""Regenerate Python protobuf bindings from the canonical .proto files (closes #8).

Run this whenever ``daqifi/_internal/proto/*.proto`` changes:

    python3 -m pip install --user grpcio-tools           # one-time, if missing
    python3 scripts/regenerate_protobuf.py

The regenerated ``*_pb2.py`` files are checked into the repo so that
``pip install daqifi`` doesn't require ``protoc`` on the install host.

Generator: ``grpc_tools.protoc`` (ships with grpcio-tools, includes its own
embedded ``protoc`` binary). We don't depend on a system-installed
``protoc`` so contributors only need ``pip install grpcio-tools``.

No gRPC service stubs (``*_pb2_grpc.py``) are emitted: the .proto file
defines messages only — no ``service`` blocks. If a future .proto adds
an RPC service definition, also pass ``--grpc_python_out=PROTO_DIR`` to
``protoc.main`` and check the generated stubs in.
"""
from __future__ import annotations

import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
PROTO_DIR = REPO_ROOT / "daqifi" / "_internal" / "proto"


def main() -> int:
    try:
        from grpc_tools import protoc
    except ImportError:
        print(
            "grpcio-tools is required. Install with: "
            "pip install grpcio-tools",
            file=sys.stderr,
        )
        return 2

    proto_files = sorted(PROTO_DIR.glob("*.proto"))
    if not proto_files:
        print(f"No .proto files found in {PROTO_DIR}", file=sys.stderr)
        return 1

    for proto in proto_files:
        print(f"Regenerating bindings for {proto.relative_to(REPO_ROOT)}")
        rc = protoc.main([
            "grpc_tools.protoc",
            f"--proto_path={PROTO_DIR}",
            f"--python_out={PROTO_DIR}",
            str(proto.name),
        ])
        if rc != 0:
            print(f"protoc failed for {proto} (rc={rc})", file=sys.stderr)
            return rc

    print(f"Regenerated {len(proto_files)} binding(s) in {PROTO_DIR}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
