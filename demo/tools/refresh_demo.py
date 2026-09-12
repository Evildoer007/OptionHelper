"""Rebuild the current App presentation and publish identical local Demo copies."""
from datetime import datetime
from pathlib import Path
import hashlib
import os
import shutil
import subprocess
import sys

SOURCE = Path(os.environ.get("OPTIONHELPER_SOURCE_ROOT", Path.home() / "Desktop/OptionHelper")).resolve()
CANONICAL = SOURCE / "demo"
MIRROR = Path.home() / "Desktop/demo/OptionHelper demo"


def file_hashes(root):
    return {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(root.rglob("*")) if path.is_file()}


def refresh_demo():
    """The project Demo owns the build; the desktop copy always comes from it."""
    subprocess.run([sys.executable, str(CANONICAL / "tools/build_current_ui.py")], check=True)
    if not (CANONICAL / "public/source-manifest.json").is_file():
        raise RuntimeError("缺少前端来源清单，不能同步Demo。")
    if MIRROR.is_symlink() or CANONICAL.is_symlink():
        raise RuntimeError("Demo目录不能是符号链接。")
    snapshot = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    backup = SOURCE / "result/demo-sync-backups" / snapshot / "desktop-before"
    if MIRROR.exists():
        backup.parent.mkdir(parents=True)
        shutil.move(str(MIRROR), str(backup))
    try:
        shutil.copytree(CANONICAL, MIRROR, ignore=shutil.ignore_patterns("node_modules", "__pycache__", ".DS_Store", ".wrangler"))
        canonical_hashes = {name: digest for name, digest in file_hashes(CANONICAL).items()
                            if not any(part in {"node_modules", "__pycache__", ".DS_Store", ".wrangler"} for part in Path(name).parts)}
        if canonical_hashes != file_hashes(MIRROR):
            raise RuntimeError("两份Demo文件校验不一致。")
    except Exception:
        # Preserve the failed copy as well as the user's previous directory.
        if MIRROR.exists():
            shutil.move(str(MIRROR), str(backup.parent / "failed-copy"))
        if backup.exists():
            shutil.move(str(backup), str(MIRROR))
        raise
    print(f"两份Demo已同步：{len(canonical_hashes)}个文件逐一通过SHA-256校验。")
    if backup.exists():
        print(f"旧桌面Demo保留在：{backup}")


if __name__ == "__main__":
    refresh_demo()
