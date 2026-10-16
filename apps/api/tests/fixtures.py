"""Build GitHub-style tarballs in memory for tests."""

import io
import tarfile

PREFIX = "owner-repo-0123abc/"
SHA = "a" * 40
SHA2 = "b" * 40

APP_PY = b'''import os


def greet(name):
    """Say hello."""
    return f"hello {name}"


class Greeter:
    def __init__(self, prefix):
        self.prefix = prefix

    def greet(self, name):
        return self.prefix + name
'''

INDEX_TS = b"""export interface User { id: number }

export const add = (a: number, b: number): number => a + b;

export class Store {
  get(id: number): User { return { id } }
}
"""

SAMPLE_FILES: dict[str, bytes] = {
    "README.md": b"# Sample\n\nA fixture repository.\n",
    "src/app.py": APP_PY,
    "src/broken.py": b"def oops(:\n    return\n",
    "web/index.ts": INDEX_TS,
    ".env.example": b"API_KEY=\n",
    "node_modules/lib/index.js": b"module.exports = 1\n",
    "dist/bundle.js": b"console.log(1)\n",
    ".env": b"API_KEY=real\n",
    "config/secrets.yaml": b"token: x\n",
    "leak.py": b'AWS = "AKIAABCDEFGHIJKLMNOP"\n',
    "logo.png": b"\x89PNG\r\n\x1a\n\x00\x00binary",
    "big.txt": b"x" * 6000 + b"\n",
    "package-lock.json": b"{}\n",
}

EXPECTED_INCLUDED = {"README.md", "src/app.py", "src/broken.py", "web/index.ts", ".env.example"}
EXPECTED_EXCLUDED = {
    "node_modules/lib/index.js": "ignored_path",
    "dist/bundle.js": "ignored_path",
    ".env": "secret_path",
    "config/secrets.yaml": "secret_path",
    "leak.py": "secret_content",
    "logo.png": "binary",
    "big.txt": "too_large",
    "package-lock.json": "generated",
    "link-to-passwd": "unsupported",
}


def make_tarball(files: dict[str, bytes] | None = None, *, hostile: bool = True) -> bytes:
    files = SAMPLE_FILES if files is None else files
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz", format=tarfile.PAX_FORMAT) as tar:
        tar.addfile(_dir(PREFIX))
        for path, data in files.items():
            info = tarfile.TarInfo(PREFIX + path)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        if hostile:
            link = tarfile.TarInfo(PREFIX + "link-to-passwd")
            link.type, link.linkname = tarfile.SYMTYPE, "/etc/passwd"
            tar.addfile(link)
            for name in (PREFIX + "../escape.py", "/abs/path.py"):
                evil = tarfile.TarInfo(name)
                evil.size = 2
                tar.addfile(evil, io.BytesIO(b"x\n"))
    return buf.getvalue()


def _dir(name: str) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.type = tarfile.DIRTYPE
    return info
