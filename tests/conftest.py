"""Top-level pytest configuration shared by every test directory."""


def pytest_configure(config):
    """Register custom marks the test suite uses.

    Tests under tests/portomatic-generated/ apply
    ``@pytest.mark.portomatic_generated`` so they can be filtered/run
    separately from hand-written tests. Registering it here suppresses
    the ``PytestUnknownMarkWarning`` that otherwise fires once per file.
    """
    config.addinivalue_line(
        "markers",
        "portomatic_generated: test was scaffolded by the portomatic apply prompt",
    )
