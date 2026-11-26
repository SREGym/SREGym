"""Assemble a job JAR with stable entry metadata and the pinned shaded connector."""

import sys
from pathlib import Path
from zipfile import ZipFile, ZipInfo

classes, output, *connector = sys.argv[1:]


def add_entry(jar, name, data):
    entry = ZipInfo(name, (2024, 1, 1, 0, 0, 0))
    entry.external_attr = 0o644 << 16
    jar.writestr(entry, data)


with ZipFile(output, "w") as jar:
    manifest = "META-INF/MANIFEST.MF"
    if connector:
        with ZipFile(connector[0]) as source:
            add_entry(jar, manifest, source.read(manifest))
            for name in sorted(source.namelist()):
                if name != manifest and not name.endswith("/"):
                    add_entry(jar, name, source.read(name))
    else:
        add_entry(jar, manifest, b"Manifest-Version: 1.0\r\n\r\n")
    root = Path(classes)
    for path in sorted(root.rglob("*.class")):
        add_entry(jar, path.relative_to(root).as_posix(), path.read_bytes())
