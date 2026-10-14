"""Private in-process proof of a fresh FILE_BLOB hash over exact immutable bytes.

Tuple-compatible records are not a serialized trust format. Public tuple inputs
are always rehashed. Only this constructor publishes identity-bound receipts.
"""
from dataclasses import dataclass
import hashlib
from weakref import WeakValueDictionary


@dataclass(frozen=True, slots=True, weakref_slot=True, eq=False)
class _Proof:
    owner_id: int
    digest: str
    key: bytes
    data: bytes


_proofs = WeakValueDictionary()


class _VerifiedBlob(tuple):
    def __new__(cls, *args, **kwargs):
        raise TypeError('verified blobs require the private validating constructor')

    def __setattr__(self, name, value):
        raise TypeError('verified blob is immutable')

    def __del__(self):
        # Remove the identity before it can be reused, even if someone retains
        # the proof itself. No proof or registry reference owns the tuple.
        try:
            _proofs.pop(id(self), None)
        except (NameError, AttributeError):
            pass  # interpreter module teardown


def _verified_file_blob(digest, data):
    if type(digest) is not str or len(digest) != 64 or any(c not in '0123456789abcdef' for c in digest):
        raise ValueError('invalid canonical blob digest')
    if type(data) is not bytes:
        raise TypeError('verified blob requires exact immutable bytes')
    key = hashlib.sha256(data).digest()
    if key.hex() != digest:
        raise ValueError('blob does not match its sha256')
    record = tuple.__new__(_VerifiedBlob, (digest, data))
    proof = _Proof(id(record), digest, key, data)
    object.__setattr__(record, '_proof', proof)
    _proofs[id(record)] = proof
    return record


def _verified_payload(record):
    """Return trusted (key, exact bytes), None for ordinary inputs; reject forgeries."""
    if not isinstance(record, _VerifiedBlob):
        return None
    if type(record) is not _VerifiedBlob or tuple.__len__(record) != 2:
        raise ValueError('invalid verified blob type or shape')
    proof = _proofs.get(id(record))
    if proof is None or proof.owner_id != id(record):
        raise ValueError('unregistered verified blob')
    try:
        attached = object.__getattribute__(record, '_proof')
    except AttributeError:
        raise ValueError('verified blob proof missing') from None
    digest, data = tuple.__getitem__(record, 0), tuple.__getitem__(record, 1)
    if (attached is not proof or type(digest) is not str or type(data) is not bytes
            or digest != proof.digest or data is not proof.data
            or type(proof.key) is not bytes or len(proof.key) != 32 or proof.key.hex() != digest):
        raise ValueError('verified blob proof does not bind its immutable contents')
    return proof.key, data
