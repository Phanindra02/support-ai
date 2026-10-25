"""The load test calls real endpoints, so a renamed route can't quietly turn it into a 404 test.

The file is read, not imported: importing Locust patches the standard library for gevent, which
would change how every other test runs.
"""

import ast

from app.config import PROJECT_ROOT
from app.main import app

LOCUSTFILE = PROJECT_ROOT / "scripts" / "locustfile.py"


def requests_made():
    """(method, path) for each self.client.get or .post call in the locustfile."""
    calls = []
    for node in ast.walk(ast.parse(LOCUSTFILE.read_text())):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in {"get", "post"}
            and ast.unparse(node.func.value) == "self.client"
        ):
            calls.append((node.func.attr.upper(), node.args[0].value))
    return calls


def test_the_load_test_logs_in_then_reads_the_dashboard():
    assert requests_made() == [
        ("POST", "/auth/token"),
        ("GET", "/stats"),
        ("GET", "/tickets"),
        ("GET", "/tickets"),
        ("GET", "/health"),
    ]


def test_every_path_the_load_test_calls_is_a_route_with_that_method():
    # The OpenAPI schema lists every path with its methods, the included routers' too.
    paths = app.openapi()["paths"]
    routes = {(method.upper(), path) for path, methods in paths.items() for method in methods}
    for request in requests_made():
        assert request in routes


def test_the_password_comes_from_the_environment():
    source = LOCUSTFILE.read_text()
    assert 'env_var="LOADTEST_EMAIL"' in source
    assert 'env_var="LOADTEST_PASSWORD", default="", is_secret=True' in source
