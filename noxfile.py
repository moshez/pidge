import functools
import os

import nox

nox.options.envdir = "build/nox"
nox.options.sessions = ["lint", "tests", "mypy", "docs"]

VERSIONS = ["3.12", "3.13", "3.14"]


@nox.session(python=VERSIONS)
def tests(session):
    tmpdir = session.create_tmp()
    session.install("-r", "requirements-tests.txt")
    session.install("-e", ".")
    tests = session.posargs or ["pidge.tests"]
    session.run(
        "coverage",
        "run",
        "--branch",
        "--source=pidge",
        "--omit=**/__main__.py",
        "-m",
        "virtue",
        *tests,
        env=dict(COVERAGE_FILE=os.path.join(tmpdir, "coverage"), TMPDIR=tmpdir),
    )
    fail_under = "--fail-under=100"
    session.run(
        "coverage",
        "report",
        fail_under,
        "--show-missing",
        "--skip-covered",
        env=dict(COVERAGE_FILE=os.path.join(tmpdir, "coverage")),
    )


@nox.session(python=VERSIONS[-1])
def build(session):
    session.install("build")
    session.run("python", "-m", "build", "--wheel")


@nox.session(python=VERSIONS[-1])
def lint(session):
    files = ["src/", "noxfile.py"]
    session.install("-r", "requirements-lint.txt")
    session.install("-e", ".")
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
        "src/",
    )


@nox.session(python=VERSIONS[-1])
def mypy(session):
    session.install("-r", "requirements-mypy.txt")
    session.install("-r", "requirements-tests.txt")
    session.install("-e", ".")
    session.run(
        "mypy",
        "--strict",
        "--disallow-any-explicit",
        "src/",
    )


@nox.session(python=VERSIONS[-1])
def docs(session):
    """Build the documentation."""
    output_dir = os.path.abspath(os.path.join(session.create_tmp(), "output"))
    doctrees, html = map(
        functools.partial(os.path.join, output_dir), ["doctrees", "html"]
    )
    session.run("rm", "-rf", output_dir, external=True)
    session.install("-r", "requirements-docs.txt")
    session.install("-e", ".")
    sphinx = ["sphinx-build", "-b", "html", "-W", "-d", doctrees, ".", html]
    session.cd("doc")
    session.run(*sphinx)


@nox.session(python=VERSIONS[-1])
def refresh_deps(session):
    """Refresh the requirements-*.txt files"""
    session.install("pip-tools")
    for deps in ["tests", "docs", "lint", "mypy"]:
        session.run(
            "pip-compile",
            "--upgrade",
            "--extra",
            deps,
            "pyproject.toml",
            "--output-file",
            f"requirements-{deps}.txt",
        )
    # The pinned runtime dependencies baked into the Lambda container image.
    session.run(
        "pip-compile",
        "--upgrade",
        "pyproject.toml",
        "--output-file",
        "requirements-lambda.txt",
    )
