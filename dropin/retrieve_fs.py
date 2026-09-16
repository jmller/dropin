"""Descriptor-bound restore staging and destination identity.

Paths are labels for reporting, never extraction/cleanup authorities. Identity
checks detect observed renames; they cannot promise permanence after the check.
"""
from contextlib import ExitStack
import os
from pathlib import Path
import stat
import uuid

from .retrieve_acquire import AcquisitionGuard


DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


def identity(info):
    return info.st_dev, info.st_ino


def entry_info(fd, name):
    try:
        return os.stat(name, dir_fd=fd, follow_symlinks=False)
    except FileNotFoundError:
        return None


def matches(fd, name, expected):
    actual = entry_info(fd, name)
    return actual is not None and identity(actual) == identity(expected)


def _report_match(fd, name, expected):
    # Diagnostic rendering must never replace the primary retrieval failure.
    try:
        return matches(fd, name, expected)
    except OSError:
        return False


class Destination:
    def __init__(self, path):
        self.path = Path(path).absolute()
        self.stack = ExitStack()
        self.edges = []
        self.aside = None
        self.aside_info = None
        self.published_info = None
        self.publication_attempted = False
        self.name = None

    def open_fd(self, name, flags, **kwargs):
        fd = os.open(name, flags, **kwargs)
        self.stack.callback(os.close, fd)
        return fd

    def __enter__(self):
        try:
            # Pin the requested destination first, then bind its whole spelling
            # to root. Existing symlink components are allowed but their link
            # identities and resolved directories must remain unchanged.
            self.fd = self.open_fd(self.path, os.O_RDONLY | os.O_DIRECTORY)
            parent = self.open_fd('/', DIRECTORY_FLAGS)
            for name in self.path.parts[1:]:
                link = os.stat(name, dir_fd=parent, follow_symlinks=False)
                child = self.open_fd(name, os.O_RDONLY | os.O_DIRECTORY, dir_fd=parent)
                self.edges.append((parent, name, link, os.fstat(child)))
                parent = child
            if identity(os.fstat(parent)) != identity(os.fstat(self.fd)):
                raise OSError('destination changed while opening; absolute paths uncertain')
            self.check()
            return self
        except BaseException:
            self.stack.close()
            raise

    def __exit__(self, *args):
        self.stack.close()

    def check(self):
        try:
            for parent, name, link, directory in self.edges:
                if not matches(parent, name, link) or identity(os.stat(name, dir_fd=parent)) != identity(directory):
                    raise OSError('directory identity differs')
        except OSError as error:
            raise OSError(f'destination or ancestor changed: {self.path}; absolute paths uncertain ({error})') from error

    def check_artifacts(self):
        if self.published_info is not None and not matches(self.fd, self.name, self.published_info):
            raise OSError('published artifact changed; destination artifact path uncertain')
        if self.aside_info is not None:
            self.aside.check()
            if not matches(self.aside.fd, self.name, self.aside_info):
                raise OSError('original aside artifact changed; destination aside path uncertain')

    def retained(self):
        # Even a path that currently matches is only a last-checked label. If
        # ancestry changed, no portable stdlib facility can recover its new name.
        location = f'destination directory identity {identity(os.fstat(self.fd))}'
        notes = []
        if self.published_info is not None:
            present = _report_match(self.fd, self.name, self.published_info)
            state = 'retained' if present else 'published but no longer identifiable at its last-known name; current location uncertain'
            notes.append(f'verified artifact {state}, identity {identity(self.published_info)}; last-known relative name in pinned {location}: {self.name!r}; last-known path {self.path / self.name}; absolute path uncertain if externally renamed')
        elif self.publication_attempted:
            notes.append(f'publication was attempted in {location}; artifact location uncertain')
        if self.aside_info is not None:
            present = _report_match(self.fd, self.aside.name, self.aside.info) and _report_match(self.aside.fd, self.name, self.aside_info)
            state = 'retained' if present else 'moved aside but no longer identifiable at its last-known name; current location uncertain'
            notes.append(f'previous destination entry {state}, identity {identity(self.aside_info)}; last-known relative name in pinned {location}: {self.aside.name}/{self.name}; last-known path {self.path / self.aside.name / self.name}; absolute path uncertain if externally renamed')
        return '; '.join(notes)


class OwnedDirectory:
    def __init__(self, destination, prefix, *, parent_fd=None):
        self.parent_fd = destination.fd if parent_fd is None else parent_fd
        self.name = prefix + uuid.uuid4().hex
        self.label = destination.path / self.name
        self.acquired = False
        self.transferred = False
        try:
            # Linux proves creation provenance before granting authority. Darwin
            # uses the documented local-user boundary, then applies the same
            # private/empty/identity checks before any payload write.
            with AcquisitionGuard(self.parent_fd, self.name) as guard:
                os.mkdir(self.name, 0o700, dir_fd=self.parent_fd)
                candidate_fd = destination.open_fd(self.name, DIRECTORY_FLAGS, dir_fd=self.parent_fd)
                candidate = os.fstat(candidate_fd)
                if (not stat.S_ISDIR(candidate.st_mode)
                        or candidate.st_uid != os.geteuid()
                        or stat.S_IMODE(candidate.st_mode) & 0o077):
                    raise OSError('candidate is not a private owned directory')
                with os.scandir(candidate_fd) as entries:
                    if next(entries, None) is not None:
                        raise OSError('candidate is not empty')
                guard.verify()
                destination.check()
                if not matches(self.parent_fd, self.name, candidate):
                    raise OSError('candidate identity changed')
                self.fd = candidate_fd
                self.info = candidate
                self.acquired = True
        except (OSError, NotImplementedError) as error:
            self.acquired = False
            # Destination owns any opened fd, including on constructor failure.
            # Never chmod, unlink, or rmdir an uncertain candidate/original.
            raise OSError(f'owned directory acquisition refused: {error}; candidate last-known path {self.label}; any created directory retained, path uncertain; no cleanup attempted') from error

    def check(self):
        if not self.acquired:
            raise OSError('directory acquisition incomplete; refusing write/cleanup authority')
        if self.info.st_uid != os.geteuid() or not matches(self.parent_fd, self.name, self.info):
            raise OSError(f'owned staging/aside identity changed: {self.label}; retained directory identity {identity(self.info)}, path uncertain; refusing unknown path')


def _empty(fd):
    # The caller has restored access to this directory BEFORE enumeration.
    with os.scandir(fd) as entries:
        for entry in entries:
            info = entry.stat(follow_symlinks=False)
            if stat.S_ISDIR(info.st_mode):
                # Supported stdlib no-follow chmod; no pathname/follow fallback
                # if a platform cannot provide it. This also unlocks mode 000.
                os.chmod(entry.name, 0o700, dir_fd=fd, follow_symlinks=False)
                child = os.open(entry.name, DIRECTORY_FLAGS, dir_fd=fd)
                try:
                    if identity(os.fstat(child)) != identity(info):
                        raise OSError('cleanup child identity changed; refusing unknown directory')
                    _empty(child)
                finally:
                    os.close(child)
                if not matches(fd, entry.name, info):
                    raise OSError('cleanup child identity changed; refusing unknown directory')
                os.rmdir(entry.name, dir_fd=fd)
            else:
                if not matches(fd, entry.name, info):
                    raise OSError('cleanup entry identity changed; refusing unknown entry')
                os.unlink(entry.name, dir_fd=fd)


def cleanup(stage):
    if stage.transferred:
        return  # Never chmod or traverse a published root, even after failure.
    stage.check()
    os.fchmod(stage.fd, 0o700)  # Root first: os.walk cannot enter a mode-000 root.
    _empty(stage.fd)
    stage.check()
    os.rmdir(stage.name, dir_fd=stage.parent_fd)
