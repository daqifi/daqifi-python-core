"""
Setup configuration for DAQiFi Python API.

Installation:
    pip install -e .
"""

from setuptools import setup, find_packages
from pathlib import Path

# Read version from version.py
version_dict = {}
with open("daqifi/version.py") as f:
    exec(f.read(), version_dict)

# Read long description from README
readme = Path("README.md").read_text(encoding="utf-8")

setup(
    name="daqifi",
    version=version_dict["__version__"],
    author=version_dict["__author__"],
    author_email="support@daqifi.com",
    description="Python API for DAQiFi Nyquist devices",
    long_description=readme,
    long_description_content_type="text/markdown",
    url="https://github.com/daqifi/daqifi-python",
    packages=find_packages(exclude=["tests", "examples"]),
    classifiers=[
        "Development Status :: 3 - Alpha",
        "Intended Audience :: Developers",
        "Intended Audience :: Science/Research",
        "License :: OSI Approved :: MIT License",
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.9",
        "Programming Language :: Python :: 3.10",
        "Programming Language :: Python :: 3.11",
        "Programming Language :: Python :: 3.12",
        "Topic :: Scientific/Engineering",
        "Topic :: Software Development :: Embedded Systems",
        "Topic :: System :: Hardware",
    ],
    # ⛔ THE FLOOR IS 3.9 BECAUSE THE PROTOBUF FLOOR FORCES IT. These two
    # declarations are coupled, and the coupling is invisible unless you look:
    # protobuf's 5.x -> 6.x major is ALSO a Requires-Python boundary.
    # Measured, from pypi.org per release:
    #     protobuf 5.28.0 / 5.29.0  Requires-Python >=3.8
    #     protobuf 6.30.0 and later Requires-Python >=3.9
    # Since the generated bindings pin the runtime to >=6.31.1 (see
    # install_requires below), no protobuf admitting 3.8 is installable. A
    # python_requires of ">=3.8" would advertise an interpreter on which
    # `pip install` resolves protobuf, finds no candidate, and FAILS OUTRIGHT
    # -- worse than not advertising it. tests/test_dependency_declarations.py
    # asserts this pairing so a future floor bump cannot silently re-open it.
    #
    # The original reason for a floor also still holds and is unchanged:
    # ported modules use typing.Protocol / runtime_checkable, unavailable in
    # 3.7. (Python 3.7 reached EOL June 2023; 3.8 reached EOL October 2024.)
    python_requires=">=3.9",
    install_requires=[
        "pyserial>=3.5",
        # protobuf is required by the generated DaqifiOutMessage_pb2
        # module (daqifi/_internal/proto/) that DaqifiDevice's default
        # message factory imports to decode device status / streaming
        # frames.
        #
        # ⛔ THE FLOOR IS ENFORCED AT IMPORT, so it is not a preference.
        # DaqifiOutMessage_pb2.py opens with
        #     ValidateProtobufRuntimeVersion(PUBLIC, 6, 31, 1, '', ...)
        # and google/protobuf/runtime_version.py raises VersionError unless the
        # installed runtime's (MAJOR, MINOR, PATCH) is >= that gencode tuple.
        # Measured against the real rule, the boundary is exact:
        #     protobuf 5.28.3 -> VersionError    6.31.0 -> VersionError
        #     protobuf 6.31.1 -> OK              6.33.6 -> OK
        # So the floor must track the gencode stamp, which is what
        # tests/test_dependency_declarations.py now enforces -- regenerating
        # the bindings with a newer protoc raises the floor and the test says
        # so, instead of the declaration quietly understating it.
        #
        # An earlier revision of this comment read "requires protobuf 5.28+
        # (protoc 6.31.x output)", which conflated the GENERATOR version with
        # the RUNTIME floor and understated the floor by a whole major
        # version: >=5.28 permitted a 5.x runtime that the bindings reject at
        # import. Because every use of the generated module is a
        # function-local import, that failed at first device use rather than
        # at install.
        #
        # ⚠ The <7.0 ceiling is a deliberate guard, NOT a measured bound: this
        # runtime's checker has no gencode-too-old branch (a 7.x runtime reads
        # as OK today), but that is 6.33.6's policy answering for 7.0's, which
        # it cannot do. Protobuf's cross-version guarantee is a limited
        # window, so a major bump is reviewed rather than assumed compatible.
        "protobuf>=6.31.1,<7.0",
        # ifaddr is a small pure-Python NIC enumerator used by
        # WiFiDeviceFinder (port of daqifi-core PR #179/#180): the
        # finder needs to broadcast on every real LAN interface so
        # discovery works on multi-NIC hosts (Wi-Fi + Ethernet, hosts
        # with VPN/virtual adapters that mask the real LAN). Falls
        # back to single-socket discovery when ifaddr is missing,
        # but having it as a hard dep keeps the multi-NIC path
        # unconditionally available out of the box.
        "ifaddr>=0.2",
    ],
    extras_require={
        "dev": [
            "pytest>=7.0",
            "pytest-cov>=3.0",
            # Required for the async hardware tests in
            # tests/integration/hardware/test_async_stream_manager.py.
            # Without it, modules using `pytest.mark.asyncio` and
            # `async def` test functions error at collection time.
            "pytest-asyncio>=0.21",
            # Required to regenerate the Python protobuf bindings via
            # ``python3 scripts/regenerate_protobuf.py`` whenever the
            # ``daqifi/_internal/proto/*.proto`` files change.
            "grpcio-tools>=1.60",
            "black>=22.0",
            "mypy>=0.950",
            "flake8>=4.0",
        ],
        "testing": [
            "pyyaml>=5.4",
        ],
    },
    entry_points={
        "console_scripts": [
            # Future CLI tools can be added here
            # "daqifi-discover=daqifi.cli:discover",
        ],
    },
    include_package_data=True,
    zip_safe=False,
)
