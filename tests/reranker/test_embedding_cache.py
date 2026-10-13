import struct
import numpy as np
import pytest
from zot2dailypaper.reranker.embedding_cache import EmbeddingCache


def test_persistent_cache_exact_roundtrip_without_plaintext(tmp_path):
    namespace = {'model':'test','revision':'immutable','prompt':'document','dtype':'float32'}
    text = 'PRIVATE_ZOTERO_ABSTRACT_SENTINEL'
    vector = np.array([1.0, 2.0], dtype=np.float32)
    cache = EmbeddingCache(namespace, tmp_path)
    cache.put(text, vector)
    restored = EmbeddingCache(namespace, tmp_path)
    assert np.array_equal(restored.get(text), vector)
    assert restored.get(text + '!') is None
    for file in tmp_path.rglob('*.npz'):
        assert text.encode() not in file.read_bytes()
        assert file.stat().st_mode & 0o077 == 0


@pytest.mark.parametrize('change', [{'revision':'new'}, {'model':'other'}, {'prompt':'query'}, {'dtype':'float64'}, {'task':'other'}])
def test_namespace_changes_invalidate(tmp_path, change):
    ns = {'model':'m','revision':'r','prompt':'doc','dtype':'float32'}
    EmbeddingCache(ns, tmp_path).put('text', np.array([1.0]))
    assert EmbeddingCache(ns | change, tmp_path).get('text') is None


def test_corrupt_entry_recomputed(tmp_path):
    cache = EmbeddingCache({}, tmp_path)
    cache.put('text', np.array([1.0]))
    next(tmp_path.rglob('*.npz')).write_bytes(b'broken')
    restored = EmbeddingCache({}, tmp_path)
    assert restored.get('text') is None
    restored.put('text', np.array([2.0]))
    assert np.array_equal(EmbeddingCache({}, tmp_path).get('text'), [2.0])


@pytest.mark.parametrize('corruption', ['compression', 'encryption'])
def test_corrupt_zip_member_encoding_is_recomputed(tmp_path, corruption):
    cache = EmbeddingCache({}, tmp_path, dimension=2, dtype='float32')
    vector = np.array([1, 2], dtype=np.float32)
    cache.put('text', vector)
    target = cache.directory / (cache.key('text') + '.npz')
    damaged = bytearray(target.read_bytes())
    # Toggle one ZIP header bit, preserving the NPY data and checksums.
    for signature, compression_offset, flags_offset in ((b'PK\x03\x04', 8, 6), (b'PK\x01\x02', 10, 8)):
        position = 0
        while (position := damaged.find(signature, position)) >= 0:
            struct.pack_into('<H', damaged, position + (compression_offset if corruption == 'compression' else flags_offset),
                             64 if corruption == 'compression' else 1)
            position += 4
    target.write_bytes(damaged)
    fresh = EmbeddingCache({}, tmp_path, dimension=2, dtype='float32')
    assert fresh.get('text') is None
    assert fresh.stats['corrupt'] == 1 and fresh.stats['misses'] == 1
    fresh.put('text', vector)
    assert np.array_equal(EmbeddingCache({}, tmp_path, dimension=2, dtype='float32').get('text'), vector)


@pytest.mark.parametrize('vector', [np.array([0.0]), np.array([np.nan]), np.array([[1.0]]), np.array([1])])
def test_invalid_vectors_rejected(vector):
    with pytest.raises(ValueError): EmbeddingCache({}).put('text', vector)


def test_disk_write_failure_retains_memory(tmp_path, monkeypatch):
    cache = EmbeddingCache({}, tmp_path)
    def fail(*args): raise OSError('disk full')
    monkeypatch.setattr('zot2dailypaper.reranker.embedding_cache.os.replace', fail)
    cache.put('text', np.array([1.0]))
    assert np.array_equal(cache.get('text'), [1.0])
    assert not list(cache.directory.iterdir())


def test_unavailable_directory_uses_memory(tmp_path):
    file = tmp_path / 'file'
    file.write_text('not a directory')
    cache = EmbeddingCache({}, file)
    cache.put('text', np.array([1.0]))
    assert cache.directory is None and np.array_equal(cache.get('text'), [1.0])


def test_private_partition_and_dimension_dtype_limits(tmp_path):
    cache=EmbeddingCache({'model':'fixed'},tmp_path,dimension=2,dtype='float32')
    assert cache.directory.parent.name=='private'
    for vector in (np.ones(3,dtype=np.float32),np.ones(2,dtype=np.float64)):
        with pytest.raises(ValueError):cache.put('private',vector)
    cache.put('private',np.ones(2,dtype=np.float32))
    assert not (tmp_path/'public').exists()
    assert cache.stats['writes']==1
    fresh=EmbeddingCache({'model':'fixed'},tmp_path,dimension=2,dtype='float32')
    assert fresh.get('private') is not None and fresh.stats['disk_hits']==1
    assert fresh.get('private') is not None and fresh.stats['memory_hits']==1


def test_oversized_and_wrong_dimension_disk_entries_are_misses(tmp_path):
    import hashlib
    cache=EmbeddingCache({},tmp_path,dimension=2,dtype='float32')
    path=cache.directory/(cache.key('text')+'.npz')
    vector=np.ones(3,dtype=np.float32)
    np.savez(path,vector=vector,checksum=hashlib.sha256(vector.tobytes()).hexdigest())
    assert cache.get('text') is None and cache.stats['corrupt']==1
    path.write_bytes(b'x'*(cache.max_bytes+1))
    assert cache.get('text') is None and cache.stats['corrupt']==2
