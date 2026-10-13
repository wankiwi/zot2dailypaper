"""Optional local-only vector cache. Never included in workflow state/artifacts."""
import hashlib
import io
import json
import math
import os
import zipfile
from pathlib import Path
from tempfile import NamedTemporaryFile

import numpy as np
from loguru import logger


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()


def valid_vector(vector):
    return isinstance(vector, np.ndarray) and vector.ndim == 1 and vector.size > 0 and np.issubdtype(vector.dtype, np.floating) and np.isfinite(vector).all() and bool(np.linalg.norm(vector))


def validate_array_archive(source, max_bytes):
    """Bound NPY allocations as well as ZIP sizes before NumPy reads arrays."""
    with zipfile.ZipFile(source) as archive:
        entries = archive.infolist()
        if sorted(i.filename for i in entries) != ['checksum.npy', 'vector.npy']:
            raise ValueError('Unexpected cache fields')
        if sum(i.file_size for i in entries) > max_bytes:
            raise ValueError('Oversized expanded cache entry')
        for entry in entries:
            with archive.open(entry) as stream:
                version = np.lib.format.read_magic(stream)
                if version not in ((1, 0), (2, 0)):
                    raise ValueError('Unsupported array format')
                reader = np.lib.format.read_array_header_1_0 if version == (1, 0) else np.lib.format.read_array_header_2_0
                shape, _, dtype = reader(stream)
                size = math.prod(shape) * dtype.itemsize
                if dtype.hasobject or size > max_bytes or size != entry.file_size-stream.tell():
                    raise ValueError('Invalid or oversized array allocation')


class EmbeddingCache:
    def __init__(self, namespace, directory=None, *, dimension=None, dtype=None):
        self.namespace = digest(namespace)
        self.dimension, self.dtype = dimension, np.dtype(dtype) if dtype else None
        self.stats = dict(memory_hits=0, disk_hits=0, misses=0, corrupt=0, writes=0)
        self.max_bytes = (dimension * (self.dtype.itemsize if self.dtype else 8) + 8192) if dimension else 1048576
        self.memory = {}
        self.directory = Path(directory).expanduser() / "private" / self.namespace if directory else None
        if self.directory:
            try:
                self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            except OSError:
                logger.warning('Local embedding cache directory unavailable; using memory only')
                self.directory = None

    def key(self, text):
        return digest([self.namespace, text])

    def get(self, text):
        key = self.key(text)
        if key in self.memory:
            self.stats["memory_hits"] += 1
            return self.memory[key]
        if self.directory:
            path = self.directory / (key + '.npz')
            if path.exists():
                try:
                    if path.stat().st_size > self.max_bytes:
                        raise ValueError('Oversized cache entry')
                    validate_array_archive(path, self.max_bytes)
                    with np.load(path, allow_pickle=False) as stored:
                        vector = stored['vector']
                        checksum = str(stored['checksum'].item())
                    if not self.valid(vector) or hashlib.sha256(vector.tobytes()).hexdigest() != checksum:
                        raise ValueError('Invalid cached embedding')
                    try:
                        os.utime(path, None)  # Encrypted snapshots retain recently used vectors.
                    except OSError:
                        pass
                    self.memory[key] = vector
                    self.stats["disk_hits"] += 1
                    return vector
                # ZIP raises RuntimeError (including NotImplementedError) for
                # encrypted members or unsupported compression methods.
                except (OSError, ValueError, KeyError, EOFError, RuntimeError, zipfile.BadZipFile):
                    self.stats['corrupt'] += 1
                    logger.warning('Ignoring corrupt local embedding cache entry; recomputing')
        self.stats["misses"] += 1
        return None

    def valid(self, vector):
        return valid_vector(vector) and (self.dimension is None or vector.size == self.dimension) and (self.dtype is None or vector.dtype == self.dtype)

    def put(self, text, vector):
        vector = np.asarray(vector)
        if not self.valid(vector):
            raise ValueError('Local encoder returned an invalid embedding')
        key = self.key(text)
        self.memory[key] = vector.copy()
        if self.directory:
            buffer = io.BytesIO()
            np.savez(buffer, vector=vector, checksum=hashlib.sha256(vector.tobytes()).hexdigest())
            temporary = None
            try:
                with NamedTemporaryFile(dir=self.directory, delete=False) as file:
                    temporary = Path(file.name)
                    file.write(buffer.getvalue())
                os.replace(temporary, self.directory / (key + '.npz'))
                self.stats["writes"] += 1
            except OSError:
                logger.warning('Unable to persist local embedding cache; retaining in-memory result')
            finally:
                if temporary:
                    temporary.unlink(missing_ok=True)
