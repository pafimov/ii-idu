import argparse
import hashlib
import zipfile
from pathlib import Path


root = Path(__file__).resolve().parents[2]
parser = argparse.ArgumentParser()
parser.add_argument("--output", type=Path, default=root / "output" / "mvp" / "ii-idu-sandbox.zip")
args = parser.parse_args()
args.output.parent.mkdir(parents=True, exist_ok=True)
allowed = {".py", ".js", ".css", ".html", ".svg", ".csv", ".md", ".command"}
files = [p for p in (root / "mvp").rglob("*") if p.is_file() and p.suffix in allowed and not {"data", "__pycache__"}.intersection(p.relative_to(root / "mvp").parts)]
files.extend(p for p in (root / ".gitignore", root / "Makefile", root / "README.md") if p.is_file())
files.extend((root / "output" / "requirements" / name) for name in
             ("2.1_PRD.docx", "2.2_SRS.docx", "2.3_UserStories.docx", "2.4_RTM.docx"))
files.append(root / "mvp" / "docs" / "sandbox-demo.jpg")
with zipfile.ZipFile(args.output, "w", zipfile.ZIP_DEFLATED) as archive:
    for path in sorted(files):
        archive.write(path, path.relative_to(root))
with zipfile.ZipFile(args.output) as archive:
    if archive.testzip():
        raise RuntimeError("Повреждён архив")
print(args.output)
print(f"{len(files)} files, {args.output.stat().st_size} bytes")
print(hashlib.sha256(args.output.read_bytes()).hexdigest())
