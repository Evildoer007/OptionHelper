"""Validate frontend module delivery against the HTTP asset allowlist."""
import ast
from pathlib import Path
import posixpath
import re


def frontend_assets(app_root: Path) -> frozenset[str]:
    tree = ast.parse((app_root / 'backend/app_server.py').read_text(encoding='utf-8'))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'FRONTEND_ASSETS' for t in node.targets):
            return frozenset(ast.literal_eval(node.value.args[0]))
    raise ValueError('Frontend asset allowlist not found')


def verify_frontend_assets(app_root: Path) -> None:
    allowed = frontend_assets(app_root)
    root = app_root / 'frontend'
    errors = []
    for relative in sorted(allowed):
        path = root / relative
        if not path.is_file():
            errors.append(f'{relative}: missing file')
            continue
        text = path.read_text(encoding='utf-8')
        refs = re.findall(r'''["'](/app/frontend/[^"'\s?#]+)["']''', text)
        if path.suffix == '.js':
            refs += re.findall(r'''(?:\bfrom\s*|\bimport\s*\(?\s*)["'](\.{1,2}/[^"']+)["']''', text)
        for ref in refs:
            target = ref.removeprefix('/app/frontend/') if ref.startswith('/app/frontend/') else posixpath.normpath(posixpath.join(posixpath.dirname(relative), ref))
            if target not in allowed:
                errors.append(f'{relative} -> {target}: not served by App')
    if errors:
        raise ValueError('Frontend delivery validation failed:\n' + '\n'.join(errors))
