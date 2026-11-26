"""Build an explicit allowlisted source archive, with no personal deployment data."""
import argparse
from hashlib import sha256
import json
from pathlib import Path
import re
from zipfile import ZipFile, ZIP_DEFLATED

ROOT = Path(__file__).resolve().parents[1]
VERSION = "0.3.0"
SINGLE = ("README.md", "LICENSE", "pyproject.toml", ".env.example", "SECURITY.md",
          "docs/接入说明.md",
          "scripts/build_release.py", "scripts/evaluate.py", "scripts/experiment.py", "scripts/prepare_demo.py",
          "scripts/check_host_integration.py")
GITHUB_ONLY = (".gitignore", "CONTRIBUTING.md", ".github/workflows/tests.yml")


def build(destination, target_type="github"):
    selected = [ROOT / name for name in SINGLE]
    if target_type == "github":
        selected.extend(ROOT / name for name in GITHUB_ONLY)
    web = ROOT / "papertrader" / "web"
    selected.extend(web / name for name in (
        "index.html", "app.css", "app.js", "assets/bg-snow-deep.jpg",
        "vendor/echarts.min.js", "vendor/LICENSE", "vendor/NOTICE",
    ))
    for directory in ("papertrader", "tests", "examples"):
        selected.extend(path for path in (ROOT / directory).rglob("*")
                        if path.is_file() and (path.suffix in (".py", ".md", ".html", ".css", ".js", ".jpg")
                                               or path.name in ("LICENSE", "NOTICE"))
                        and web not in path.parents
                        and "__pycache__" not in path.parts)
    selected = sorted(set(selected))
    private_patterns = [
        re.compile(r"sk-[A-Za-z0-9_-]{20,}"),
        re.compile(r"-----BEGIN (?:RSA |OPENSSH |EC )?PRIVATE KEY-----"),
        re.compile(r"82\.156\.124\.121|chris_" + r"deploy_key|\u5171\u751f|\u597d\u611f\u5ea6"),
    ]
    manifest = {}
    for path in selected:
        if not path.is_file() or path.is_symlink():
            raise ValueError(f"Missing or unsafe release file: {path.relative_to(ROOT)}")
        text = path.read_text(encoding="utf-8") if path.suffix not in (".jpg", ".png") else ""
        if any(pattern.search(text) for pattern in private_patterns):
            raise ValueError(f"Review possible private content: {path.relative_to(ROOT)}")
        manifest[path.relative_to(ROOT).as_posix()] = sha256(path.read_bytes()).hexdigest()
    destination.mkdir(parents=True, exist_ok=True)
    suffix = "source" if target_type == "github" else "submission"
    target = destination / f"papertrader-{VERSION}-{suffix}.zip"
    with ZipFile(target, "w", ZIP_DEFLATED) as archive:
        prefix = f"papertrader-{VERSION}/"
        for path in selected:
            archive.write(path, prefix + path.relative_to(ROOT).as_posix())
        archive.writestr(prefix + "MANIFEST.json", json.dumps(manifest, indent=2, ensure_ascii=False))
    checksum = sha256(target.read_bytes()).hexdigest()
    target.with_suffix(".zip.sha256").write_text(checksum + "  " + target.name + "\n", encoding="ascii")
    return target, len(selected), checksum


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "dist")
    default_target = "github" if (ROOT / ".github/workflows/tests.yml").is_file() else "submission"
    parser.add_argument("--target", choices=("github", "submission"), default=default_target)
    args = parser.parse_args()
    target, count, digest = build(args.output.resolve(), args.target)
    print(json.dumps({"archive": str(target), "files": count, "sha256": digest}))


if __name__ == "__main__":
    main()
