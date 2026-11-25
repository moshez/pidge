"""Checks for pidge-client. Run from this directory: ``nox``."""

import os

import nox

nox.options.envdir = "../build/nox-client"
nox.options.sessions = ["lint", "tests", "mypy"]

VERSIONS = ["3.12", "3.13", "3.14"]


@nox.session(python=VERSIONS)
def tests(session):
    """Run the tests (against pidge in-process) with full coverage."""
    tmpdir = session.create_tmp()
    session.install("-r", "../requirements-tests.txt")
    session.install("-e", "..", "-e", ".")
    tests = session.posargs or ["pidge_client.tests"]
    coverage = dict(COVERAGE_FILE=os.path.join(tmpdir, "coverage"))
    session.run(
        "coverage",
        "run",
        "--branch",
        "--source=pidge_client",
        "-m",
        "virtue",
        *tests,
        env=dict(coverage, TMPDIR=tmpdir),
    )
    session.run(
        "coverage",
        "report",
        "--fail-under=100",
        "--show-missing",
        "--skip-covered",
        env=coverage,
    )


@nox.session(python=VERSIONS[-1])
def bare(session):
    """Import and run the views with nothing installed, only PYTHONPATH."""
    bare = dict(PYTHONPATH="src")
    session.run("python", "-c", "import pidge_client.calls", env=bare)
    session.run("python", "-m", "pidge_client.views", "--help", env=bare)


@nox.session(python=VERSIONS[-1])
def e2e(session):
    """Check the client against a deployed endpoint.

    Needs PIDGE_ENDPOINT and PIDGE_TOKEN (from get_session_token).
    """
    session.run(
        "python", "-m", "unittest", "-v", "e2e.test_live", env=dict(PYTHONPATH="src")
    )


@nox.session(python=VERSIONS[-1])
def build(session):
    """Build the wheel."""
    session.install("build")
    session.run("python", "-m", "build", "--wheel")


@nox.session(python=VERSIONS[-1])
def lint(session):
    """Run black and stolid."""
    files = ["src/", "e2e/", "noxfile.py"]
    session.install("-r", "../requirements-lint.txt")
    session.install("-e", "..", "-e", ".")
    session.run("black", "--check", "--diff", *files)
    session.run(
        "python",
        "-m",
        "stolid",
        "--max-line-length=88",
        "--ignore=E203,E503,W503",
        "--style=google",
        "--skip-checking-short-docstrings=False",
        "--arg-type-hints-in-docstring=False",
        "--check-return-types=False",
        "--check-yield-types=False",
        # Test cases and fakes are concrete by nature, and repeat assertion shapes.
        "--per-file-ignores=src/*/tests/*,e2e/*:SLD801,SLD303,SLD802,SLD803,SLD205",
        "src/",
        "e2e/",
    )


@nox.session(python=VERSIONS[-1])
def mypy(session):
    """Type-check strictly."""
    session.install("-r", "../requirements-mypy.txt")
    session.install("-r", "../requirements-tests.txt")
    session.install("-e", "..", "-e", ".")
    session.run(
        "mypy",
        "--strict",
        "--disallow-any-explicit",
        "src/",
        "e2e/",
        env=dict(MYPYPATH="../src"),
    )
