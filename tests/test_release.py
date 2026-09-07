from pathlib import Path


def test_bootstrap_is_bundled_with_its_license():
    import crowbarr

    static = Path(crowbarr.__file__).parent / "static"
    assert (static / "vendor" / "bootstrap.min.css").stat().st_size > 100000
    assert "MIT" in (static / "vendor" / "BOOTSTRAP-LICENSE").read_text()


def test_docker_context_is_allowlisted():
    root = Path(__file__).resolve().parents[1]
    ignore = (root / ".dockerignore").read_text()
    assert ignore.startswith("*\n")
    assert "server-infra" not in ignore
    assert "!crowbarr/**" in ignore
