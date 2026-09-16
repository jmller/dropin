"""Verified retrieval without publication-state writes.

Only a private destination-side staging directory receives untrusted bytes.
Manifest directories are created first, regular members are streamed and hashed,
links are created last, and atomic no-replace rename publishes only after EOF
and successful engine context exit. Stdout is deliberately untrusted until exit.
"""
from contextlib import closing
from dataclasses import dataclass
import hashlib
import json
import os
import sqlite3
import stat
import tarfile

from .capture.extract import MANIFEST_FIELDS
from .engine.interface import EngineError
from .pipeline.verify import CHUNK_SIZE, PayloadError, _member_rel_path, recompute_manifest_hash
from .retrieve_rename import rename_noreplace
from .retrieve_fs import (Destination, OwnedDirectory, DIRECTORY_FLAGS, cleanup as _cleanup,
                          entry_info, matches)


class RetrieveError(Exception):
    def __init__(self, kind, reason):
        super().__init__(reason)
        self.kind = kind


@dataclass(frozen=True)
class Target:
    occurrence: sqlite3.Row
    entry: sqlite3.Row
    snapshot: str

    @property
    def name(self):
        return self.entry['rel_path'].rsplit('/', 1)[-1] or self.occurrence['item_name']

    @property
    def kind(self):
        if not self.entry['rel_path'] and self.occurrence['kind'] == 'bundle':
            return 'bundle'
        return self.entry['entry_type']


def resolve(db, archive_path):
    row = db.execute('''SELECT o.* FROM occurrence o JOIN entry e ON e.occ_id=o.occ_id
        WHERE e.archive_path=? AND o.confirmed_attempt_id IS NOT NULL''', (archive_path,)).fetchone()
    if row is None:
        raise RetrieveError('missing', f'unknown confirmed archive path: {archive_path}')
    attempt = db.execute("SELECT snapshot_id FROM publication_attempt WHERE attempt_id=? AND occ_id=? AND outcome='confirmed'",
                         (row['confirmed_attempt_id'], row['occ_id'])).fetchone()
    if attempt is None or not attempt['snapshot_id']:
        raise RetrieveError('missing', f'no confirmed snapshot for {archive_path}')
    if row['kind'] == 'bundle':
        entry = db.execute("SELECT * FROM entry WHERE occ_id=? AND rel_path=''", (row['occ_id'],)).fetchone()
    else:
        entry = db.execute('SELECT * FROM entry WHERE archive_path=?', (archive_path,)).fetchone()
    if entry is None:
        raise RetrieveError('corrupt', 'manifest root is missing')
    target = Target(row, entry, attempt['snapshot_id'])
    _safe_relative(target.name)
    if '/' in target.name:
        raise RetrieveError('corrupt', 'manifest root name must be one basename')
    return target


def _safe_relative(path):
    if not path or path.startswith('/') or '\x00' in path or any(p in ('', '.', '..') for p in path.split('/')):
        raise RetrieveError('corrupt', f'unsafe manifest/member path: {path!r}')


def _entries(db, occ_id, root):
    # SQL prefix equality, not LIKE (filenames may contain % and _).
    return db.execute('''SELECT * FROM entry WHERE occ_id=? AND
        (rel_path=? OR substr(rel_path,1,?)=?) ORDER BY rel_path''',
        (occ_id, root, len(root) + 1 if root else 0, root + '/' if root else ''))


def _relative(path, root):
    return path[len(root) + 1:] if root and path != root else '' if path == root else path


def _manifest(db, target):
    occ = target.occurrence
    root = db.execute("SELECT * FROM entry WHERE occ_id=? AND rel_path=''", (occ['occ_id'],)).fetchone()
    count = db.execute('SELECT count(*) FROM entry WHERE occ_id=?', (occ['occ_id'],)).fetchone()[0]
    if root is None or count != occ['entry_count']:
        raise RetrieveError('corrupt', 'manifest entry count/root differs')
    digest = root['sha256'] if occ['kind'] == 'file' else recompute_manifest_hash(db, occ['occ_id'])
    if digest != occ['root_sha256']:
        raise RetrieveError('corrupt', 'manifest hash differs from occurrence')


def _copy_verified(stream, output, row):
    digest, size = hashlib.sha256(), 0
    for chunk in iter(lambda: stream.read(CHUNK_SIZE), b''):
        output.write(chunk)
        digest.update(chunk)
        size += len(chunk)
    if digest.hexdigest() != row['sha256'] or size != row['size_bytes']:
        raise RetrieveError('corrupt', f"{row['rel_path'] or '.'}: content hash/size differs from record")


class _StrictTarInfo(tarfile.TarInfo):
    """Tarfile normally accepts missing EOF or a bad late header as normal EOF.

    Intercept header errors before TarFile.next can swallow them. Require both
    zero end blocks and only zero padding through transport EOF, including the
    tar stream reader's already-buffered bytes. Do not concatenate archives.
    """
    @classmethod
    def fromtarfile(cls, archive):
        try:
            return super().fromtarfile(archive)
        except tarfile.EOFHeaderError:
            second = archive.fileobj.read(tarfile.BLOCKSIZE)
            if second != b'\0' * tarfile.BLOCKSIZE:
                raise RetrieveError('corrupt', 'tar stream lacks two zero end blocks')
            for chunk in iter(lambda: archive.fileobj.read(CHUNK_SIZE), b''):
                if chunk.strip(b'\0'):
                    raise RetrieveError('corrupt', 'nonzero data after tar end marker')
            raise
        except tarfile.HeaderError as error:
            raise RetrieveError('corrupt', f'tar stream header/EOF invalid: {error}') from error


def _tree(db, engine, target, root, payload):
    occ_id = target.occurrence['occ_id']
    root_row = db.execute('SELECT * FROM entry WHERE occ_id=? AND rel_path=?', (occ_id, root)).fetchone()
    if root_row is None or root_row['entry_type'] != 'dir':
        raise RetrieveError('corrupt', 'manifest tree root is not a directory')
    expected = 0
    for row in _entries(db, occ_id, root):
        relative = _relative(row['rel_path'], root)
        if not relative:
            continue
        _safe_relative(relative)
        parent = row['rel_path'].rpartition('/')[0]
        ancestor = db.execute('SELECT entry_type FROM entry WHERE occ_id=? AND rel_path=?', (occ_id, parent)).fetchone()
        if ancestor is None or ancestor[0] != 'dir':
            raise RetrieveError('corrupt', f'{relative}: parent is not a manifest directory')
        if row['entry_type'] == 'dir':
            os.mkdir(relative, mode=0o700, dir_fd=payload)
        elif row['entry_type'] not in ('file', 'symlink'):
            raise RetrieveError('corrupt', f'{relative}: unsupported manifest type')
        expected += 1
    remote = target.occurrence['spool_path'] + ('/' + root if root else '')
    count = 0
    # Independent disk-backed scratch table: never write even a TEMP table to
    # the query-only catalog connection, and never accumulate a Python path set.
    with closing(sqlite3.connect('')) as seen:
        seen.execute('PRAGMA cache_size=-2048')
        seen.execute('PRAGMA temp_store=FILE')
        seen.execute('CREATE TABLE seen (path TEXT PRIMARY KEY)')
        with engine.dump(target.snapshot, remote, archive='tar') as stream:
            with tarfile.open(fileobj=stream, mode='r|', tarinfo=_StrictTarInfo) as archive:
                for member in archive:
                    relative = _member_rel_path(member.name, remote.lstrip('/'))
                    _safe_relative(relative)
                    rel = root + '/' + relative if root else relative
                    row = db.execute('SELECT * FROM entry WHERE occ_id=? AND rel_path=?', (occ_id, rel)).fetchone()
                    if row is None:
                        raise RetrieveError('corrupt', f'{relative}: member absent from manifest')
                    try:
                        seen.execute('INSERT INTO seen VALUES (?)', (relative,))
                    except sqlite3.IntegrityError as error:
                        raise RetrieveError('corrupt', f'{relative}: duplicate member') from error
                    count += 1
                    if member.islnk():
                        raise RetrieveError('corrupt', f'{relative}: hardlink member refused')
                    if member.isdir() and row['entry_type'] == 'dir':
                        pass
                    elif member.issym() and row['entry_type'] == 'symlink':
                        if member.linkname != row['link_target']:
                            raise RetrieveError('corrupt', f'{relative}: link target differs')
                    elif member.isreg() and row['entry_type'] == 'file':
                        if member.size != row['size_bytes'] or member.sparse is not None:
                            raise RetrieveError('corrupt', f'{relative}: size/sparse member differs')
                        with archive.extractfile(member) as source, _output(payload, relative) as output:
                            _copy_verified(source, output, row)
                            output.flush()
                            os.fsync(output.fileno())
                            os.fchmod(output.fileno(), stat.S_IMODE(row['mode']))
                    else:
                        raise RetrieveError('corrupt', f'{relative}: unsupported/mismatched member type')
                    archive.members.clear()
        if count != expected:
            raise RetrieveError('corrupt', 'manifest members missing from stream')
    # No archive symlink exists until all bytes and engine exit are verified.
    for row in _entries(db, occ_id, root):
        relative = _relative(row['rel_path'], root)
        if row['entry_type'] == 'symlink':
            os.symlink(row['link_target'], relative, dir_fd=payload)


def _output(fd, name):
    return os.fdopen(os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                             0o600, dir_fd=fd), 'wb')


def _directory_modes(db, occ_id, root, payload):
    for row in db.execute("SELECT rel_path,mode FROM entry WHERE occ_id=? AND entry_type='dir' ORDER BY rel_path DESC", (occ_id,)):
        if row['rel_path'] == root or not root or row['rel_path'].startswith(root + '/'):
            relative = _relative(row['rel_path'], root)
            mode = stat.S_IMODE(row['mode'])
            if relative:
                os.chmod(relative, mode, dir_fd=payload, follow_symlinks=False)
            else:
                os.fchmod(payload, mode)


def _publish(source, destination, name, force, stage):
    fd = destination.fd
    destination.name = name
    stage.check()
    destination.check()
    original = entry_info(fd, name)
    if original is not None:
        if not force:
            raise RetrieveError('refused', f'destination exists: {destination.path / name}')
        aside = OwnedDirectory(destination, '.dropin-aside-')
        destination.aside = aside
        move_error = None
        try:
            destination.check()
            aside.check()
            rename_noreplace(fd, name, aside.fd, name)
        except OSError as error:
            move_error = error
            raise
        finally:
            # Also account for interruption immediately after the native move.
            try:
                moved = entry_info(aside.fd, name)
                if moved is not None:
                    destination.aside_info = moved
                else:
                    aside.check()
                    os.rmdir(aside.name, dir_fd=fd)
            except OSError as error:
                raise RetrieveError('refused', f'aside move/check failed: {move_error or error}; aside cleanup failed: {error}; container last-known path {aside.label}, path uncertain if externally renamed') from error
        if not matches(aside.fd, name, original):
            raise RetrieveError('refused', f'destination target changed during force-aside; pre-move target location uncertain; aside last-known path {aside.label / name}')
    try:
        destination.check()
        stage.check()
        destination.check_artifacts()
        source_fd, source_name = source
        # A tree's pathname can be replaced after stage.check(). Its authority
        # remains the directory pinned before verification, never a fresh stat.
        source_info = (stage.info if source_fd == fd and source_name == stage.name
                       else os.stat(source_name, dir_fd=source_fd, follow_symlinks=False))
        if not matches(source_fd, source_name, source_info):
            raise OSError('publication source identity changed; verified staging artifact location uncertain')
        destination.publication_attempted = True
        try:
            rename_noreplace(source_fd, source_name, fd, name)
        finally:
            if matches(fd, name, source_info):
                destination.published_info = source_info
            if source_fd == fd and not matches(fd, stage.name, stage.info):
                # The tree root may have been published even if interrupted
                # after native success. Never clean/chmod that moved root.
                stage.transferred = True
        if destination.published_info is None:
            raise OSError('publication source identity changed during rename; verified staging artifact location uncertain')
        destination.check()
        destination.check_artifacts()
    except OSError as error:
        if destination.aside_info is not None and destination.published_info is None:
            try:
                # Rollback is descriptor-relative too. A replaced destination
                # path does not authorize touching the replacement directory.
                destination.check_artifacts()
                rename_noreplace(destination.aside.fd, name, fd, name)
                destination.aside_info = None
                destination.aside.check()
                os.rmdir(destination.aside.name, dir_fd=fd)
            except OSError as rollback_error:
                raise RetrieveError('refused', f'publication failed: {error}; rollback refused: {rollback_error}') from error
        raise


def _restore(db, engine, target, destination, force, stage):
    remote = target.occurrence['spool_path']
    rel = target.entry['rel_path']
    if rel:
        remote += '/' + rel
    digest = target.entry['sha256']
    if target.kind == 'file':
        with engine.dump(target.snapshot, remote) as stream, _output(stage.fd, 'payload') as output:
            _copy_verified(stream, output, target.entry)
            output.flush()
            os.fsync(output.fileno())
            os.fchmod(output.fileno(), stat.S_IMODE(target.entry['mode']))
        source = stage.fd, 'payload'
    else:
        payload = stage.fd
        if target.kind == 'symlink':
            os.mkdir('payload', 0o700, dir_fd=stage.fd)
            payload = destination.open_fd('payload', DIRECTORY_FLAGS, dir_fd=stage.fd)
        root = rel.rpartition('/')[0] if target.kind == 'symlink' else rel
        _tree(db, engine, target, root, payload)
        if target.kind == 'symlink':
            source = payload, _relative(rel, root)
        else:
            # Same-parent rename preserves captured mode 000/0555, unlike a
            # cross-parent move which can need write permission to update '..'.
            source = destination.fd, stage.name
            _directory_modes(db, target.occurrence['occ_id'], root, payload)
            digest = _subtree_hash(db, target.occurrence['occ_id'], root)
    _publish(source, destination, target.name, force, stage)
    result = {'written_path': str(destination.path / target.name), 'sha256': digest,
              'verified': True, 'kind': target.kind}
    if destination.aside_info is not None:
        result['aside_path'] = str(destination.path / destination.aside.name / target.name)
    return result


def _normalized(error):
    if isinstance(error, RetrieveError):
        return error
    if isinstance(error, EngineError):
        return RetrieveError(error.kind, str(error))
    if isinstance(error, (tarfile.TarError, PayloadError, EOFError)):
        return RetrieveError('corrupt', f'tar stream invalid: {error}')
    return RetrieveError('refused', str(error))


def retrieve(db, engine, archive_path, destination_dir='.', *, force=False, stdout=None):
    target = resolve(db, archive_path)
    if stdout is not None and target.kind != 'file':
        raise RetrieveError('usage', '--stdout requires one regular file')
    _manifest(db, target)
    try:
        if stdout is not None:
            remote = target.occurrence['spool_path']
            if target.entry['rel_path']:
                remote += '/' + target.entry['rel_path']
            with engine.dump(target.snapshot, remote) as stream:
                _copy_verified(stream, stdout, target.entry)
            return {'written_path': None, 'sha256': target.entry['sha256'], 'verified': True, 'kind': 'file'}
        with Destination(destination_dir) as destination:
            if entry_info(destination.fd, target.name) is not None and not force:
                raise RetrieveError('refused', f'destination exists: {destination.path / target.name}')
            stage = OwnedDirectory(destination, f".dropin-restore-{target.occurrence['occ_id']}-")
            primary = None
            try:
                result = _restore(db, engine, target, destination, force, stage)
            except (RetrieveError, EngineError, tarfile.TarError, PayloadError, EOFError, OSError, NotImplementedError) as error:
                primary = _normalized(error)
            finally:
                try:
                    _cleanup(stage)
                except (OSError, NotImplementedError) as error:
                    reason = f'cleanup failed: {error}; owned stage last-known path {stage.label}, directory identity {stage.info.st_dev, stage.info.st_ino}; leftover path uncertain if externally renamed'
                    primary = RetrieveError(primary.kind, f'{primary}; {reason}') if primary else RetrieveError('refused', reason)
            # Cleanup itself may have raced a destination/ancestor rename. No
            # successful path is reported until after this final identity check.
            try:
                destination.check()
                destination.check_artifacts()
            except OSError as error:
                primary = RetrieveError(primary.kind, f'{primary}; {error}') if primary else _normalized(error)
            if primary is not None:
                retained = destination.retained()
                if retained:
                    primary = RetrieveError(primary.kind, f'{primary}; {retained}')
                raise primary
            return result
    except (EngineError, tarfile.TarError, PayloadError, EOFError, OSError, NotImplementedError) as error:
        raise _normalized(error) from error


def _subtree_hash(db, occ_id, root):
    digest = hashlib.sha256()
    for row in _entries(db, occ_id, root):
        item = {field: row[field] for field in MANIFEST_FIELDS}
        digest.update(json.dumps(item, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode('utf-8', 'surrogateescape'))
        digest.update(b'\n')
    return digest.hexdigest()
