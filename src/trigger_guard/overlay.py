"""Apply the pinned integration to a new copy, never to an installed package."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess

PATCHES = Path(__file__).resolve().parent / "patches"


def digest(path):
    return hashlib.sha256(Path(path).read_text(encoding="utf-8").replace("\r\n", "\n").encode()).hexdigest()


def manifest():
    return json.loads((PATCHES / "manifest.json").read_text(encoding="utf-8"))


def verify_overlay(root):
    root = Path(root).resolve()
    data = manifest()
    for relative, expected in data["patched_files"].items():
        path = root / relative
        if not path.is_file() or digest(path) != expected:
            raise ValueError(f"Overlay file does not match the pinned integration: {relative}")
    return root


def prepare_overlay(source, output):
    source, output = Path(source).resolve(), Path(output).resolve()
    if not source.is_dir() or not (source / "__init__.py").is_file():
        raise ValueError("--source must be the installed vllm package directory")
    if output.exists() or output.is_relative_to(source) or source.is_relative_to(output):
        raise ValueError("--output must be a new directory separate from the source package")
    if shutil.which("git") is None:
        raise ValueError("git is required to apply and check the pinned patches")
    data = manifest()
    for relative, expected in data["source_files"].items():
        path = source.parent / relative
        if not path.is_file() or digest(path) != expected:
            raise ValueError(f"Source differs from the pinned vLLM 0.17.0 base: {relative}")
    output.mkdir(parents=True, exist_ok=False)

    def copy_file(src, dst):
        # Reuse installed extension binaries; all Python files are independent.
        if ".so" in Path(src).name:
            Path(dst).symlink_to(Path(src).resolve())
            return dst
        return shutil.copy2(src, dst)

    shutil.copytree(source, output / "vllm", copy_function=copy_file,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    # Do not let an enclosing source checkout change git-apply's working root.
    apply_env = dict(os.environ, GIT_CEILING_DIRECTORIES=str(output.parent))
    for key in ["GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"]:
        apply_env.pop(key, None)
    for name in data["patch_order"]:
        patch = PATCHES / name
        subprocess.run(["git", "apply", "--check", str(patch)], cwd=output, env=apply_env, check=True)
        subprocess.run(["git", "apply", str(patch)], cwd=output, env=apply_env, check=True)
    verify_overlay(output)
    for relative, expected in data["source_files"].items():
        if digest(source.parent / relative) != expected:
            raise RuntimeError("Source package changed while preparing the copy")
    (output / "INSTALLATION.json").write_text(json.dumps({
        "source_package": str(source), "base_commit": data["base_commit"],
        "patch_order": data["patch_order"], "verified": True,
    }, indent=2) + "\n", encoding="utf-8")
    return output
