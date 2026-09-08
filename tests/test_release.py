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


def sections(text: str) -> dict[str, str]:
    """Split the changelog into its top-level sections, keyed by heading."""
    found, heading = {}, None
    for line in text.splitlines():
        if line.startswith("## "):
            heading = line[3:].strip()
            found[heading] = []
        elif heading:
            found[heading].append(line)
    return {name: "\n".join(body) for name, body in found.items()}


def test_every_released_version_is_written_down():
    """A policy bump re-opens unresolved work in every installation, and can publish
    subtitles the previous policy refused. Nobody upgrading should have to read a diff
    to find out why their review queue refilled, so the entry is not optional."""
    from crowbarr.version import APPLICATION_VERSION, AUDIT_POLICY_VERSION

    root = Path(__file__).resolve().parents[1]
    changelog = sections((root / "CHANGELOG.md").read_text())
    assert set(changelog) >= {"Audit policy", "Application"}, "changelog lost a section"
    assert f"### {AUDIT_POLICY_VERSION}" in changelog["Audit policy"], (
        f"audit policy {AUDIT_POLICY_VERSION} has no CHANGELOG entry. A policy change "
        f"re-audits every unresolved job in every installation; say what changed."
    )
    assert f"### {APPLICATION_VERSION}" in changelog["Application"], (
        f"application {APPLICATION_VERSION} has no CHANGELOG entry."
    )


def test_the_changelog_is_reachable_from_the_readme():
    root = Path(__file__).resolve().parents[1]
    assert "CHANGELOG.md" in (root / "README.md").read_text()
