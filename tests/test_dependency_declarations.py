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
