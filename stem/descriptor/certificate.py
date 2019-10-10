# Copyright 2017-2019, Damian Johnson and The Tor Project
# See LICENSE for licensing information

"""
Parsing for `Tor Ed25519 certificates
<https://gitweb.torproject.org/torspec.git/tree/cert-spec.txt>`_, which are
used to for a variety of purposes...

  * validating the key used to sign server descriptors
  * validating the key used to sign hidden service v3 descriptors
  * signing and encrypting hidden service v3 indroductory points

.. versionadded:: 1.6.0

**Module Overview:**

::

  Ed25519Certificate - Ed25519 signing key certificate
    | +- Ed25519CertificateV1 - version 1 Ed25519 certificate
    |      |- is_expired - checks if certificate is presently expired
    |      +- validate - validates signature of a server descriptor
    |
    +- parse - reads base64 encoded certificate data

  Ed25519Extension - extension included within an Ed25519Certificate

.. data:: CertType (enum)

  Purpose of Ed25519 certificate. As new certificate versions are added this
  enumeration will expand.

  For more information see...

    * `cert-spec.txt <https://gitweb.torproject.org/torspec.git/tree/cert-spec.txt>`_ section A.1
    * `rend-spec-v3.txt <https://gitweb.torproject.org/torspec.git/tree/rend-spec-v3.txt>`_ appendix E

  ========================  ===========
  CertType                  Description
  ========================  ===========
  **SIGNING**               signing key with an identity key
  **LINK_CERT**             TLS link certificate signed with ed25519 signing key
  **AUTH**                  authentication key signed with ed25519 signing key
  **HS_V3_DESC_SIGNING**    hidden service v3 short-term descriptor signing key
  **HS_V3_INTRO_AUTH**      hidden service v3 introductory point authentication key
  **HS_V3_INTRO_ENCRYPT**   hidden service v3 introductory point encryption key
  ========================  ===========

.. data:: ExtensionType (enum)

  Recognized exception types.

  ====================  ===========
  ExtensionType         Description
  ====================  ===========
  **HAS_SIGNING_KEY**   includes key used to sign the certificate
  ====================  ===========

.. data:: ExtensionFlag (enum)

  Flags that can be assigned to Ed25519 certificate extensions.

  ======================  ===========
  ExtensionFlag           Description
  ======================  ===========
  **AFFECTS_VALIDATION**  extension affects whether the certificate is valid
  **UNKNOWN**             extension includes flags not yet recognized by stem
  ======================  ===========
"""

import base64
import binascii
import collections
import datetime
import hashlib

import stem.prereq
import stem.descriptor.server_descriptor
import stem.util.enum
import stem.util.str_tools
import stem.util

from cryptography.hazmat.primitives import serialization

ED25519_HEADER_LENGTH = 40
ED25519_SIGNATURE_LENGTH = 64
ED25519_ROUTER_SIGNATURE_PREFIX = b'Tor router descriptor signature v1'

CertType = stem.util.enum.UppercaseEnum(
  'RESERVED_0', 'RESERVED_1', 'RESERVED_2', 'RESERVED_3',
  'SIGNING', 'LINK_CERT', 'AUTH', 'RESERVED_RSA',
  'HS_V3_DESC_SIGNING', 'HS_V3_INTRO_AUTH', 'RESERVED_0A',
  'HS_V3_INTRO_ENC', 'HS_V3_NTOR_ENC')

ExtensionType = stem.util.enum.Enum(('HAS_SIGNING_KEY', 4),)
ExtensionFlag = stem.util.enum.UppercaseEnum('AFFECTS_VALIDATION', 'UNKNOWN')


class Ed25519Extension(collections.namedtuple('Ed25519Extension', ['type', 'flags', 'flag_int', 'data'])):
  """
  Extension within an Ed25519 certificate.

  :var int type: extension type
  :var list flags: extension attribute flags
  :var int flag_int: integer encoding of the extension attribute flags
  :var bytes data: data the extension concerns
  """


class Ed25519Certificate(object):
  """
  Base class for an Ed25519 certificate.

  :var int version: certificate format version
  :var unicode encoded: base64 encoded ed25519 certificate
  """

  def __init__(self, version, encoded):
    self.version = version
    self.encoded = encoded

  @staticmethod
  def parse(content):
    """
    Parses the given base64 encoded data as an Ed25519 certificate.

    :param str content: base64 encoded certificate

    :returns: :class:`~stem.descriptor.certificate.Ed25519Certificate` subclsss
      for the given certificate

    :raises: **ValueError** if content is malformed
    """

    content = stem.util.str_tools._to_unicode(content)

    if content.startswith('-----BEGIN ED25519 CERT-----\n') and content.endswith('\n-----END ED25519 CERT-----'):
      content = content[29:-27]

    try:
      decoded = base64.b64decode(content)

      if not decoded:
        raise TypeError('empty')
    except (TypeError, binascii.Error) as exc:
      raise ValueError("Ed25519 certificate wasn't propoerly base64 encoded (%s):\n%s" % (exc, content))

    version = stem.util.str_tools._to_int(decoded[0:1])

    if version == 1:
      return Ed25519CertificateV1(version, content, decoded)
    else:
      raise ValueError('Ed25519 certificate is version %i. Parser presently only supports version 1.' % version)

  @staticmethod
  def _from_descriptor(keyword, attribute):
    def _parse(descriptor, entries):
      value, block_type, block_contents = entries[keyword][0]

      # XXX ATAGAR This ValueError never actually surfaces if it triggers...
      if not block_contents or block_type != 'ED25519 CERT':
        raise ValueError("'%s' should be followed by a ED25519 CERT block, but was a %s" % (keyword, block_type))

      setattr(descriptor, attribute, Ed25519Certificate.parse(block_contents))

    return _parse

  def __str__(self):
    return '-----BEGIN ED25519 CERT-----\n%s\n-----END ED25519 CERT-----' % self.encoded


class Ed25519CertificateV1(Ed25519Certificate):
  """
  Version 1 Ed25519 certificate, which are used for signing tor server
  descriptors.

  :var CertType type: certificate purpose
  :var datetime expiration: expiration of the certificate
  :var int key_type: format of the key
  :var bytes key: key content
  :var list extensions: :class:`~stem.descriptor.certificate.Ed25519Extension` in this certificate
  :var bytes signature: certificate signature
  """

  def __init__(self, version, encoded, decoded):
    super(Ed25519CertificateV1, self).__init__(version, encoded)

    if len(decoded) < ED25519_HEADER_LENGTH + ED25519_SIGNATURE_LENGTH:
      raise ValueError('Ed25519 certificate was %i bytes, but should be at least %i' % (len(decoded), ED25519_HEADER_LENGTH + ED25519_SIGNATURE_LENGTH))

    cert_type = stem.util.str_tools._to_int(decoded[1:2])
    try:
      self.type = CertType.keys()[cert_type]
    except IndexError:
      raise ValueError('Certificate has wrong cert type')

    # Catch some invalid cert types
    if self.type in ('RESERVED_0', 'RESERVED_1', 'RESERVED_2', 'RESERVED_3'):
      raise ValueError('Ed25519 certificate cannot have a type of %i. This is reserved to avoid conflicts with tor CERTS cells.' % cert_type)
    elif self.type == ('RESERVED_RSA'):
      raise ValueError('Ed25519 certificate cannot have a type of 7. This is reserved for RSA identity cross-certification.')

    # expiration time is in hours since epoch
    try:
      self.expiration = datetime.datetime.utcfromtimestamp(stem.util.str_tools._to_int(decoded[2:6]) * 3600)
    except ValueError as exc:
      raise ValueError('Invalid expiration timestamp (%s): %s' % (exc, stem.util.str_tools._to_int(decoded[2:6]) * 3600))

    self.key_type = stem.util.str_tools._to_int(decoded[6:7])
    self.key = decoded[7:39]
    self.signature = decoded[-ED25519_SIGNATURE_LENGTH:]

    self.extensions = []
    extension_count = stem.util.str_tools._to_int(decoded[39:40])
    remaining_data = decoded[40:-ED25519_SIGNATURE_LENGTH]

    for i in range(extension_count):
      if len(remaining_data) < 4:
        raise ValueError('Ed25519 extension is missing header field data')

      extension_length = stem.util.str_tools._to_int(remaining_data[:2])
      extension_type = stem.util.str_tools._to_int(remaining_data[2:3])
      extension_flags = stem.util.str_tools._to_int(remaining_data[3:4])
      extension_data = remaining_data[4:4 + extension_length]

      if extension_length != len(extension_data):
        raise ValueError("Ed25519 extension is truncated. It should have %i bytes of data but there's only %i." % (extension_length, len(extension_data)))

      flags, remaining_flags = [], extension_flags

      if remaining_flags % 2 == 1:
        flags.append(ExtensionFlag.AFFECTS_VALIDATION)
        remaining_flags -= 1

      if remaining_flags:
        flags.append(ExtensionFlag.UNKNOWN)

      if extension_type == ExtensionType.HAS_SIGNING_KEY and len(extension_data) != 32:
        raise ValueError('Ed25519 HAS_SIGNING_KEY extension must be 32 bytes, but was %i.' % len(extension_data))

      self.extensions.append(Ed25519Extension(extension_type, flags, extension_flags, extension_data))
      remaining_data = remaining_data[4 + extension_length:]

    if remaining_data:
      raise ValueError('Ed25519 certificate had %i bytes of unused extension data' % len(remaining_data))

  def is_expired(self):
    """
    Checks if this certificate is presently expired or not.

    :returns: **True** if the certificate has expired, **False** otherwise
    """

    return datetime.datetime.now() > self.expiration

  def signing_key(self):
    """
    Provides this certificate's signing key.

    .. versionadded:: 1.8.0

    :returns: **bytes** with the first signing key on the certificate, None if
      not present
    """

    for extension in self.extensions:
      if extension.type == ExtensionType.HAS_SIGNING_KEY:
        return extension.data

    return None

  def validate(self, descriptor):
    """
    Validates our signing key and that the given descriptor content matches its
    Ed25519 signature. Supported descriptor types include...

      * server descriptors

    :param stem.descriptor.__init__.Descriptor descriptor: descriptor to validate

    :raises:
      * **ValueError** if signing key or descriptor are invalid
      * **ImportError** if cryptography module is unavailable or ed25519 is
        unsupported
    """

    if not stem.prereq._is_crypto_ed25519_supported():
      raise ImportError('Certificate validation requires the cryptography module and ed25519 support')

    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    from cryptography.exceptions import InvalidSignature

    if not isinstance(descriptor, stem.descriptor.server_descriptor.RelayDescriptor):
      raise ValueError('Certificate validation only supported for server descriptors, not %s' % type(descriptor).__name__)

    if descriptor.ed25519_master_key:
      signing_key = base64.b64decode(stem.util.str_tools._to_bytes(descriptor.ed25519_master_key) + b'=')
    else:
      signing_key = self.signing_key()

    if not signing_key:
      raise ValueError('Server descriptor missing an ed25519 signing key')

    try:
      Ed25519PublicKey.from_public_bytes(signing_key).verify(self.signature, base64.b64decode(stem.util.str_tools._to_bytes(self.encoded))[:-ED25519_SIGNATURE_LENGTH])
    except InvalidSignature:
      raise ValueError('Ed25519KeyCertificate signing key is invalid (Signature was forged or corrupt)')

    # ed25519 signature validates descriptor content up until the signature itself

    descriptor_content = descriptor.get_bytes()

    if b'router-sig-ed25519 ' not in descriptor_content:
      raise ValueError("Descriptor doesn't have a router-sig-ed25519 entry.")

    signed_content = descriptor_content[:descriptor_content.index(b'router-sig-ed25519 ') + 19]
    descriptor_sha256_digest = hashlib.sha256(ED25519_ROUTER_SIGNATURE_PREFIX + signed_content).digest()

    missing_padding = len(descriptor.ed25519_signature) % 4
    signature_bytes = base64.b64decode(stem.util.str_tools._to_bytes(descriptor.ed25519_signature) + b'=' * missing_padding)

    try:
      verify_key = Ed25519PublicKey.from_public_bytes(self.key)
      verify_key.verify(signature_bytes, descriptor_sha256_digest)
    except InvalidSignature:
      raise ValueError('Descriptor Ed25519 certificate signature invalid (Signature was forged or corrupt)')

class MyED25519Certificate(object):
  """
  This class represents an ed25519 certificate and it's made for encoding it into a string.
  We should merge this class with the one above.
  """
  def __init__(self, cert_type, expiration_date,
               cert_key_type, certified_pub_key,
               signing_priv_key, include_signing_key,
               version=1):
    """
    :var int version
    :var int cert_type
    :var CertType cert_type
    :var datetime expiration_date
    :var int cert_key_type
    :var ED25519PublicKey certified_pub_key
    :var ED25519PrivateKey signing_priv_key
    :var bool include_signing_key
    """
    self.version = version
    self.cert_type = cert_type
    self.expiration_date = expiration_date
    self.cert_key_type = cert_key_type
    self.certified_pub_key = certified_pub_key

    self.signing_priv_key = signing_priv_key
    self.signing_pub_key = signing_priv_key.public_key()

    self.include_signing_key = include_signing_key
    # XXX validate params

  def _get_certificate_signature(self, msg_body):
    return self.signing_priv_key.sign(msg_body)

  def _get_cert_extensions_bytes(self):
    """
    Build the cert extensions part of the certificate
    """
    n_extensions = 0

    # If we need to include the signing key, let's create the extension body
    #         ExtLength [2 bytes]
    #         ExtType   [1 byte]
    #         ExtFlags  [1 byte]
    #         ExtData   [ExtLength bytes]
    if self.include_signing_key:
      n_extensions += 1

      signing_pubkey_bytes = self.signing_pub_key.public_bytes(encoding=serialization.Encoding.Raw,
                                                               format=serialization.PublicFormat.Raw)

      ext_length = len(signing_pubkey_bytes)
      ext_type = 4
      ext_flags = 0
      ext_data = signing_pubkey_bytes

    # Now build the actual byte representation of any extensions
    ext_obj = bytes()
    ext_obj += n_extensions.to_bytes(1, 'big')

    if self.include_signing_key:
      ext_obj += ext_length.to_bytes(2, 'big')
      ext_obj += ext_type.to_bytes(1, 'big')
      ext_obj += ext_flags.to_bytes(1, 'big')
      ext_obj += ext_data

    return ext_obj

  def encode(self):
    """Return a bytes representation of this certificate."""
    obj = bytes()

    # Encode VERSION
    obj += self.version.to_bytes(1, 'big')

    # Encode CERT_TYPE
    try:
      cert_type_int = CertType.index_of(self.cert_type)
    except ValueError:
      raise ValueError("Bad cert type %s" % self.cert_type)

    obj += cert_type_int.to_bytes(1, 'big')

    # Encode EXPIRATION_DATE
    expiration_seconds_since_epoch = stem.util.datetime_to_unix(self.expiration_date)
    expiration_hours_since_epoch = int(expiration_seconds_since_epoch) // 3600
    obj += expiration_hours_since_epoch.to_bytes(4, 'big')

    # Encode CERT_KEY_TYPE
    obj += self.cert_key_type.to_bytes(1, 'big')

    # Encode CERTIFIED_KEY
    certified_pub_key_bytes = self.certified_pub_key.public_bytes(encoding=serialization.Encoding.Raw,
                                                          format=serialization.PublicFormat.Raw)
    assert(len(certified_pub_key_bytes) == 32)
    obj += certified_pub_key_bytes

    # Encode N_EXTENSIONS and EXTENSIONS
    obj += self._get_cert_extensions_bytes()

    # Do the signature on the body we have so far
    obj += self._get_certificate_signature(obj)

    return obj

  def encode_for_descriptor(self):
    cert_bytes = self.encode()
    cert_b64 = base64.b64encode(cert_bytes)
    cert_b64 = b'\n'.join(stem.util.str_tools._split_by_length(cert_b64, 64))
    return b'-----BEGIN ED25519 CERT-----\n%s\n-----END ED25519 CERT-----' % cert_b64
