"""Build a public source archive from an explicit allowlist, excluding local infrastructure."""

from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

from crowbarr import __version__


def main():
    root = Path(__file__).resolve().parents[1]
    files = [
        "pyproject.toml",
        "README.md",
        "LICENSE",
        "CONTRIBUTING.md",
        "PRODUCT.md",
        "DESIGN.md",
        "Dockerfile",
        "Dockerfile.cuda",
        "compose.yaml",
        "compose.cuda.yaml",
        ".env.example",
        ".gitignore",
        ".dockerignore",
    ]
    selected = [root / name for name in files]
    for name in ("crowbarr", "docs", "tests", "integrations", "scripts", ".github"):
        selected.extend(
            p
            for p in (root / name).rglob("*")
            if p.is_file() and "__pycache__" not in p.parts and p.suffix not in {".pyc", ".pyo"}
        )
    output = root / "dist" / f"crowbarr-{__version__}-source.zip"
    output.parent.mkdir(exist_ok=True)
    with ZipFile(output, "w", ZIP_DEFLATED) as archive:
        for path in sorted(selected):
            if path.is_symlink():
                raise ValueError(f"Refusing to export symlink: {path.relative_to(root)}")
            archive.write(path, f"crowbarr-{__version__}/{path.relative_to(root)}")
    print(f"Created {output.name}: {len(selected)} public files")


if __name__ == "__main__":
    main()
