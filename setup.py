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
        "Programming Language :: Python :: 3.7",
        "Programming Language :: Python :: 3.8",
        "Programming Language :: Python :: 3.9",
        "Programming Language :: Python :: 3.10",
        "Programming Language :: Python :: 3.11",
        "Topic :: Scientific/Engineering",
        "Topic :: Software Development :: Embedded Systems",
        "Topic :: System :: Hardware",
    ],
    python_requires=">=3.7",
    install_requires=[
        "pyserial>=3.5",
    ],
    extras_require={
        "dev": [
            "pytest>=7.0",
            "pytest-cov>=3.0",
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
