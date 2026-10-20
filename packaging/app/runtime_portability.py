"""Runtime isolation and binary compatibility gates for self-contained App delivery."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import json
import os
import plistlib
import re
import struct
import subprocess
import sys

MACOS_MINIMUM_VERSION = '14.0'
MACOS_ARCHITECTURE = 0x0100000C


def clean_runtime_environment(root: Path) -> dict[str, str]:
    """Give a packaged process a fresh home, no developer settings or package paths."""
    root = root.resolve()
    for name in ('home', 'tmp'):
        (root / name).mkdir(parents=True, exist_ok=True)
    return {'HOME': str(root / 'home'), 'TMPDIR': str(root / 'tmp') + '/',
            'PATH': '/usr/bin:/bin:/usr/sbin:/sbin', 'LANG': 'en_US.UTF-8',
            'PYTHONTZPATH': '', 'PYTHONDONTWRITEBYTECODE': '1'}


def isolated_macos_command(command: list[str], *, blocked_roots=()) -> list[str]:
    """Deny developer installations and external Python/Node even by absolute path."""
    if sys.platform != 'darwin':
        raise RuntimeError('macOS隔离验收必须在macOS执行')
    sandbox = Path('/usr/bin/sandbox-exec')
    if not sandbox.is_file():
        raise RuntimeError('缺少macOS沙盒工具，不能将本机检查标记为隔离验收通过')
    roots = {Path(sys.prefix).resolve(), Path('/opt/anaconda3'), Path('/opt/homebrew'),
             Path('/usr/local'), Path.home() / '.tinyshare',
             Path.home() / 'Library/Application Support/OptionHelper',
             *(Path(p).resolve() for p in blocked_roots)}
    rules = ' '.join('(subpath ' + json.dumps(str(p)) + ')' for p in sorted(roots))
    profile = ('(version 1)(allow default)(deny file-read* ' + rules + ')'
               '(deny process-exec (regex #".*/(node|nodejs|python[0-9.]*|conda|pip[0-9.]*|npm|npx)$"))')
    return [str(sandbox), '-p', profile, *command]


def _version(value: str) -> tuple[int, ...]:
    return tuple(int(item) for item in value.split('.'))


def verify_macos_binary_compatibility(bundle: Path) -> dict[str, object]:
    """Check every Mach-O, rather than trusting the native shell's Info.plist alone."""
    info = plistlib.loads((bundle / 'Contents/Info.plist').read_bytes())
    declared = str(info.get('LSMinimumSystemVersion', ''))
    if declared != MACOS_MINIMUM_VERSION:
        raise RuntimeError('App最低系统版本必须与构建目标一致')
    binaries = []
    for path in bundle.rglob('*'):
        if not path.is_file() or path.is_symlink():
            continue
        with path.open('rb') as stream:
            header = stream.read(8)
        if header[:4] == b'\xcf\xfa\xed\xfe':
            if struct.unpack('<I', header[4:8])[0] != MACOS_ARCHITECTURE:
                raise RuntimeError(f'App包含非ARM64二进制：{path.relative_to(bundle)}')
            binaries.append(path)
    if not binaries:
        raise RuntimeError('App未找到可验证的ARM64二进制')
    def minimum(path):
        result = subprocess.run(['/usr/bin/otool', '-l', str(path)],
                                capture_output=True, text=True, check=True)
        versions = re.findall(r'\bminos\s+([0-9.]+)', result.stdout)
        versions += re.findall(r'cmd LC_VERSION_MIN_MACOSX\s+cmdsize \d+\s+version ([0-9.]+)', result.stdout)
        if not versions:
            raise RuntimeError(f'二进制缺少最低系统声明：{path.relative_to(bundle)}')
        if any(_version(value) > _version(declared) for value in versions):
            raise RuntimeError(f'二进制要求的macOS高于{declared}：{path.relative_to(bundle)}={versions}')
        return max(versions, key=_version)
    with ThreadPoolExecutor(max_workers=6) as pool:
        versions = list(pool.map(minimum, binaries))
    return {'minimum_macos': declared, 'architecture': 'arm64', 'binaries_checked': len(binaries),
            'highest_binary_requirement': max(versions, key=_version)}
