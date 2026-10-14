"""Private identity-bound receipt for one freshly verified immutable page.

This is an in-process representation, never a serialized trust flag. The only
constructor computes the full content hash; ordinary Bundle payloads remain
untrusted. Receipts own their proofs; the registry does not own receipts.
"""
# Values are immutable tuples of the original binding; they never own the
# bytes-subclass receipt. Its destructor removes the entry before id reuse.
# Keeping the binding itself immutable also prevents swapping a digest through
# object.__setattr__ on a nominally frozen dataclass.
_proofs = {}


class _VerifiedPage(bytes):
    def __new__(cls, *args, **kwargs):
        raise TypeError('verified pages require the private validating constructor')

    def __setattr__(self, name, value):
        raise TypeError('verified page is immutable')

    def __del__(self):
        try:
            _proofs.pop(id(self), None)
        except (NameError, AttributeError):
            pass


def _verified_page(vaddr, key, data):
    from kptas.types import PAGE_SIZE, page_key
    if (type(vaddr) is not int or not 0 <= vaddr < 1 << 32
            or vaddr % PAGE_SIZE or type(key) is not bytes or len(key) != 16
            or type(data) is not bytes or len(data) != PAGE_SIZE):
        raise ValueError('invalid immutable page identity or payload')
    if page_key(data) != key:
        raise ValueError(f'page {vaddr:#x} hash mismatch')
    record = bytes.__new__(_VerifiedPage, data)
    proof = (id(record), vaddr, key, data)
    object.__setattr__(record, '_proof', proof)
    _proofs[id(record)] = proof
    return record


def _verified_page_payload(record, vaddr, key):
    """Exact registered receipt → original bytes; ordinary inputs → None."""
    if not isinstance(record, _VerifiedPage):
        return None
    if type(record) is not _VerifiedPage:
        raise ValueError('invalid verified page type')
    proof = _proofs.get(id(record))
    if proof is None or proof[0] != id(record):
        raise ValueError('unregistered verified page')
    try:
        attached = object.__getattribute__(record, '_proof')
    except AttributeError:
        raise ValueError('verified page proof missing') from None
    if (attached is not proof or type(vaddr) is not int or vaddr != proof[1]
            or type(key) is not bytes or key != proof[2]
            or type(proof[3]) is not bytes or len(proof[3]) != 4096
            or bytes.__len__(record) != 4096
            or bytes.__eq__(record, proof[3]) is not True):
        raise ValueError('verified page proof does not bind its immutable identity and bytes')
    return proof[3]
