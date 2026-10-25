"""The two runtime-dependency declarations must agree.

WHY THIS TEST EXISTS, and why it checks AGREEMENT rather than either file:

``setup.py``'s ``install_requires`` and ``requirements.txt`` both claim to state
this package's runtime dependencies, and they had drifted. ``requirements.txt``
listed ``pyserial`` alone while ``install_requires`` also carried ``protobuf``
and ``ifaddr`` -- so ``pip install -r requirements.txt`` produced a package that
imports cleanly and then fails at first real device use, because every use of
the generated protobuf module is a FUNCTION-LOCAL import and nothing fails until
the feature is reached.

⛔ THE DISAGREEMENT WAS THE DEFECT, not either file. Fixing ``requirements.txt``
once leaves nothing stopping the next edit from re-opening the gap, which is why
this is a test and not a one-time correction. Pinning CI to ``pip install .``
(which honours ``install_requires``) makes CI *correct* while leaving the drift
*undetected* -- a green gate that proves nothing about the dependency set a
``requirements.txt`` user actually gets.

⚠ ``setup.py`` is parsed with ``ast``, never imported or executed: importing it
would invoke ``setup()``. So this test reads the declaration as data.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SETUP_PY = REPO_ROOT / "setup.py"
REQUIREMENTS = REPO_ROOT / "requirements.txt"

# Only the NAME and the version specifier are compared. Ordering, comments and
# whitespace are presentation; the requirement set is the claim.
_SPEC = re.compile(r"^\s*([A-Za-z0-9._-]+)\s*(.*?)\s*$")


def _normalise(req: str) -> tuple[str, str]:
    """('ProtoBuf', '>=5.28,<7.0') -> ('protobuf', '>=5.28,<7.0')."""
    m = _SPEC.match(req)
    assert m, f"unparseable requirement: {req!r}"
    name, spec = m.group(1), m.group(2)
    return name.lower().replace("_", "-"), spec.replace(" ", "")


def _install_requires() -> list[str]:
    """``install_requires`` from setup.py, read as data via ast."""
    tree = ast.parse(SETUP_PY.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Name) and func.id == "setup"):
            continue
        for kw in node.keywords:
            if kw.arg == "install_requires":
                # A literal list is required: anything computed cannot be read
                # as data, and silently returning [] would make this test pass
                # vacuously. Refuse instead.
                assert isinstance(kw.value, ast.List), (
                    "setup.py's install_requires is not a literal list, so it "
                    "cannot be compared as data. Keep it a plain list, or "
                    "teach this test the new shape -- do NOT let it read as "
                    "empty."
                )
                return [ast.literal_eval(e) for e in kw.value.elts]
    raise AssertionError("no setup(install_requires=[...]) found in setup.py")


def _requirements_txt() -> list[str]:
    out = []
    for raw in REQUIREMENTS.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if line:
            out.append(line)
    return out


PROTO_DIR = REPO_ROOT / "daqifi" / "_internal" / "proto"


def _gencode_versions() -> list[tuple[Path, tuple[int, int, int]]]:
    """The ``(major, minor, patch)`` each generated binding demands at import.

    Read with ``ast``, like ``install_requires`` and for the same reason: the
    value is a declaration, so it is read as data rather than by importing the
    module (importing it would run the very validation under test).
    """
    out = []
    for pb2 in sorted(PROTO_DIR.glob("*_pb2.py")):
        tree = ast.parse(pb2.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "ValidateProtobufRuntimeVersion"
            ):
                continue
            # args: (domain, major, minor, patch, suffix, location)
            major, minor, patch = (
                ast.literal_eval(a) for a in node.args[1:4]
            )
            out.append((pb2, (major, minor, patch)))
            break
    return out


def _declared_protobuf_floor() -> tuple[int, int, int]:
    """The ``>=`` floor declared for protobuf, as a comparable tuple."""
    for req in _install_requires():
        name, spec = _normalise(req)
        if name != "protobuf":
            continue
        m = re.search(r">=\s*([0-9]+(?:\.[0-9]+)*)", spec)
        assert m, (
            f"protobuf is declared as {spec!r} with no >= floor. The floor is "
            "enforced at import by the generated bindings, so it cannot be "
            "left open."
        )
        parts = [int(p) for p in m.group(1).split(".")]
        parts += [0] * (3 - len(parts))
        return tuple(parts[:3])
    raise AssertionError("protobuf is not declared in install_requires")


def test_declared_protobuf_floor_covers_the_generated_bindings():
    """⛔ The declared floor must not be lower than what the bindings enforce.

    ``DaqifiOutMessage_pb2.py`` opens with
    ``ValidateProtobufRuntimeVersion(PUBLIC, 6, 31, 1, ...)``, and
    ``google/protobuf/runtime_version.py`` raises ``VersionError`` unless the
    installed runtime's ``(MAJOR, MINOR, PATCH)`` is ``>=`` that tuple. So the
    floor is not a preference -- a declaration below it permits an install the
    package rejects at import.

    This existed: the floor read ``>=5.28`` against a ``6.31.1`` gencode,
    understating it by a major version. Measured against the real rule,
    5.28.3 / 6.30.0 / 6.31.0 all raise and 6.31.1 is the first that passes.

    ⭐ It is a TEST rather than a corrected number because the number drifts by
    construction: regenerating the bindings with a newer protoc rewrites the
    gencode stamp, and nothing else would notice the floor no longer covering
    it. Coupling the two makes a regeneration say so.
    """
    gencodes = _gencode_versions()
    # Vacuity guard, same reasoning as the one below: an empty list would make
    # the loop pass having compared nothing.
    assert gencodes, (
        f"found no ValidateProtobufRuntimeVersion call under {PROTO_DIR} -- "
        "either the bindings moved or the generator stopped emitting the "
        "check. Do NOT let this read as 'nothing to verify'."
    )

    floor = _declared_protobuf_floor()
    too_low = [(p, g) for p, g in gencodes if floor < g]

    assert not too_low, (
        "the declared protobuf floor is lower than the generated bindings "
        f"enforce at import.\n  declared floor: {'.'.join(map(str, floor))}\n"
        + "".join(
            f"  {p.relative_to(REPO_ROOT)} requires >= "
            f"{'.'.join(map(str, g))}\n"
            for p, g in too_low
        )
        + "A runtime satisfying the declaration would raise VersionError on "
        "first use of the generated module. Raise the floor in setup.py AND "
        "requirements.txt to the highest gencode version above."
    )


# Requires-Python per protobuf release, measured from pypi.org (the authority;
# this is not inferable from the package itself):
#     protobuf 5.28.0, 5.29.0   -> >=3.8
#     protobuf 6.30.0 and later -> >=3.9
# So protobuf's 5.x -> 6.x major is ALSO a Python-version boundary, which is the
# coupling this test exists to hold.
_PROTOBUF_MAJOR_MIN_PYTHON = {5: (3, 8), 6: (3, 9)}


def _python_requires_floor() -> tuple[int, int]:
    """The ``>=`` floor from setup.py's ``python_requires``, as a tuple."""
    tree = ast.parse(SETUP_PY.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "setup"
        ):
            continue
        for kw in node.keywords:
            if kw.arg == "python_requires":
                spec = ast.literal_eval(kw.value)
                m = re.search(r">=\s*([0-9]+)\.([0-9]+)", spec)
                assert m, f"python_requires={spec!r} has no >= floor to check"
                return int(m.group(1)), int(m.group(2))
    raise AssertionError("no python_requires in setup.py")


def test_python_requires_is_compatible_with_the_declared_protobuf_floor():
    """⛔ An advertised interpreter must be one the dependencies can install on.

    This broke exactly once and is the reason for the test. Raising the protobuf
    floor to ``>=6.31.1`` (correct -- the gencode enforces it at import) left
    ``python_requires=">=3.8"`` standing, and no 6.x protobuf admits 3.8. The
    result is worse than not advertising 3.8: pip accepts the package, resolves
    protobuf, finds no candidate, and the install FAILS OUTRIGHT.

    ⭐ The two declarations are forty lines apart and nothing connected them. A
    dependency floor crossing a major can move Requires-Python with it, so the
    pairing is asserted rather than remembered.
    """
    py_floor = _python_requires_floor()
    proto_floor = _declared_protobuf_floor()
    proto_major = proto_floor[0]

    assert proto_major in _PROTOBUF_MAJOR_MIN_PYTHON, (
        f"protobuf floor is {'.'.join(map(str, proto_floor))}, whose major "
        f"({proto_major}) is not in the measured Requires-Python table. "
        "Re-measure it from pypi.org for that major and extend "
        "_PROTOBUF_MAJOR_MIN_PYTHON -- do NOT drop the check, because the "
        "whole point is that a major bump can move Requires-Python."
    )

    required_py = _PROTOBUF_MAJOR_MIN_PYTHON[proto_major]
    assert py_floor >= required_py, (
        "python_requires advertises an interpreter the dependency set cannot "
        "install on.\n"
        f"  python_requires floor : {'.'.join(map(str, py_floor))}\n"
        f"  protobuf floor        : {'.'.join(map(str, proto_floor))}\n"
        f"  protobuf {proto_major}.x needs  : Python "
        f">={'.'.join(map(str, required_py))}\n"
        "Either raise python_requires (and drop the matching classifier), or "
        "lower the protobuf floor -- but the protobuf floor is pinned by the "
        "generated bindings, so in practice it is python_requires that moves."
    )


def test_both_declarations_are_non_empty():
    """⛔ THE VACUITY GUARD, and it runs first on purpose.

    Scope, measured rather than assumed -- an earlier version of this docstring
    claimed "if EITHER parse returns nothing", which is false:

        setup.py empty, requirements.txt full  -> the comparison already FAILS
        requirements.txt empty, setup.py full  -> the comparison already FAILS
        BOTH empty                             -> the comparison PASSES, vacuously

    So one empty side is caught by the agreement test itself; it is the
    both-empty case that slips through, which is what this guard is for. The
    >= 2 floor covers the subtler version: both parsers degrading to a single
    item that happens to agree, which the comparison also cannot see, because a
    set comparison of equal garbage is equal.
    """
    installed = _install_requires()
    pinned = _requirements_txt()
    assert installed, "parsed install_requires as EMPTY -- the parser is broken"
    assert pinned, "parsed requirements.txt as EMPTY -- the parser is broken"
    assert len(installed) >= 2 and len(pinned) >= 2, (
        f"expected several runtime deps on both sides, got "
        f"{len(installed)} and {len(pinned)} -- suspiciously few, check the "
        f"parsers before trusting the comparison"
    )


def test_requirements_txt_agrees_with_install_requires():
    """The two declarations must name the same requirements, with the same specs."""
    installed = {_normalise(r) for r in _install_requires()}
    pinned = {_normalise(r) for r in _requirements_txt()}

    missing_from_txt = installed - pinned
    extra_in_txt = pinned - installed

    assert not missing_from_txt and not extra_in_txt, (
        "setup.py's install_requires and requirements.txt disagree.\n"
        f"  declared in setup.py but NOT in requirements.txt: "
        f"{sorted(missing_from_txt)}\n"
        f"  in requirements.txt but NOT declared in setup.py: "
        f"{sorted(extra_in_txt)}\n"
        "Both files claim to state the runtime dependencies. A user who "
        "follows requirements.txt gets the second set; a user who runs "
        "`pip install .` gets the first. When they differ, one of those users "
        "has a broken install and neither file looks wrong on its own."
    )
