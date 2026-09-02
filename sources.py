#!/usr/bin/env python3
"""Turning a source of content into a list of files to upload.

Two sources feed the same pipeline: a folder on local disk (when the app runs
on your machine) and files dropped into the browser (when it runs on a server,
where there is no folder of yours to point at).
"""
import io
import os
import shutil
import zipfile

# Extension groups offered as filters in the UI. None = no filtering.
FILTERS = {
    "All files": None,
    "Images": {".jpg", ".jpeg", ".png", ".webp", ".gif", ".heic", ".bmp", ".tiff"},
    "Videos": {".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm"},
    "Images + videos": {".jpg", ".jpeg", ".png", ".webp", ".gif", ".heic", ".bmp",
                        ".tiff", ".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm"},
    "Documents": {".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".txt", ".csv"},
}


def human_size(n):
    """Bytes as a short human string."""
    for unit in ["B", "KB", "MB", "GB"]:
        if n < 1024 or unit == "GB":
            return "{:.1f} {}".format(n, unit) if unit != "B" else "{} B".format(int(n))
        n /= 1024.0


def scan_folder(root, exts, recurse, skip_hidden=True):
    """Return sorted (abs_path, path relative to root) pairs for files to upload."""
    found = []
    if recurse:
        for dirpath, dirnames, filenames in os.walk(root):
            if skip_hidden:
                dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            for name in sorted(filenames):
                if skip_hidden and name.startswith("."):
                    continue
                if exts and os.path.splitext(name)[1].lower() not in exts:
                    continue
                full = os.path.join(dirpath, name)
                found.append((full, os.path.relpath(full, root)))
    else:
        for name in sorted(os.listdir(root)):
            full = os.path.join(root, name)
            if not os.path.isfile(full):
                continue
            if skip_hidden and name.startswith("."):
                continue
            if exts and os.path.splitext(name)[1].lower() not in exts:
                continue
            found.append((full, name))
    return sorted(found, key=lambda t: t[1])


def stage_uploads(uploaded, workdir):
    """Write browser-uploaded files into workdir, expanding any .zip.

    A zip keeps its internal folder structure so it can be mirrored into Drive;
    loose files land at the top level. Members with absolute paths or `../`
    segments are dropped rather than allowed to escape workdir (zip-slip).
    """
    safe_root = os.path.abspath(workdir)
    for uf in uploaded:
        name = os.path.basename(uf.name)
        if name.lower().endswith(".zip"):
            with zipfile.ZipFile(io.BytesIO(uf.getbuffer())) as z:
                for member in z.infolist():
                    if member.is_dir():
                        continue
                    target = os.path.normpath(os.path.join(safe_root, member.filename))
                    if not target.startswith(safe_root + os.sep):
                        continue
                    parent = os.path.dirname(target)
                    if parent:
                        os.makedirs(parent, exist_ok=True)
                    with z.open(member) as src, open(target, "wb") as dst:
                        shutil.copyfileobj(src, dst)
        else:
            with open(os.path.join(safe_root, name), "wb") as f:
                f.write(uf.getbuffer())
