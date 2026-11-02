"""Existing hybrid encryption format; recipient private key never enters Actions."""
import base64
import hashlib
import json
import secrets
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def seal(value, public_pem, experiment, protocol_sha256):
    recipient = serialization.load_pem_public_key(public_pem)
    if not isinstance(recipient, rsa.RSAPublicKey) or recipient.key_size < 3072:
        raise ValueError('recipient_key_strength')
    header = {'schema': 1, 'algorithm': 'RSA-OAEP-SHA256+A256GCM',
              'experiment': experiment, 'protocol_sha256': protocol_sha256,
              'recipient_sha256': hashlib.sha256(public_pem).hexdigest()}
    aad = canonical(header)
    key, nonce = AESGCM.generate_key(bit_length=256), secrets.token_bytes(12)
    wrapped = recipient.encrypt(key, padding.OAEP(mgf=padding.MGF1(hashes.SHA256()),
                                                 algorithm=hashes.SHA256(), label=aad))
    encrypted = AESGCM(key).encrypt(nonce, canonical(value), aad)
    return dict(header, wrapped_key=base64.b64encode(wrapped).decode(),
                nonce=base64.b64encode(nonce).decode(), ciphertext=base64.b64encode(encrypted).decode())
