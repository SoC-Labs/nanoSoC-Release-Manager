"""Shared pytest setup for the acceptance tests (docs/contracts/interfaces.md section 17.4).

Markers:
    requirement("<ID>")  the requirement an acceptance test accepts (PR5, PR7)
    live                 needs the lab: the private service config, the registry checkout or the network.
                         A live test without its inputs FAILS; deselect with -m "not live" off the VM.

Not built yet: every acceptance test carries NOT_BUILT = xfail(raises=ImportError, strict=True).
Before each test that has a `requirement` marker, the package is imported here, so while `nanosoc_rm`
does not exist every acceptance test reports XFAIL whatever its inputs. Once the package exists:
    - a test whose module is still missing raises ImportError inside its body: XFAIL;
    - a test that fails for any other reason FAILS (red);
    - a test that passes FAILS as XPASS(strict), until its builder removes NOT_BUILT.
So a skip can never read as green, and a passing test cannot hide behind its marker.
"""
import importlib


def pytest_configure(config):
    config.addinivalue_line("markers", "requirement(id): the requirement this acceptance test accepts (PR5, PR7)")
    config.addinivalue_line("markers", "live: needs the lab (service config, registry checkout or network); "
                                       "fails without its inputs, deselect with -m 'not live'")


def pytest_runtest_setup(item):
    """Runs after pytest has evaluated the xfail marker and before any fixture is set up."""
    if item.get_closest_marker("requirement") is not None:
        try:
            importlib.import_module("nanosoc_rm")
        except ImportError as exc:
            raise ImportError("nanosoc_rm is not built yet (gate 2): %s" % exc) from exc
