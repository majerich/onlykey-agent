"""
Connection to hardware authentication device.

It is used for getting SSH public keys and ECDSA signing of server requests.
"""
import binascii
import io
import logging
import re
import hashlib

from onlykey import OnlyKey, Message

from . import formats, util
import ecdsa
import ed25519
import time

log = logging.getLogger(__name__)


class Client(object):
    """Client wrapper for SSH authentication device."""

    def __init__(self, curve=formats.CURVE_NIST256):
        """Connect to hardware device."""
        self.device_name = 'OnlyKey'
        self.ok = OnlyKey()
        self.curve = curve

    def __enter__(self):
        """Start a session, and test connection."""
        self.ok.read_string(timeout_ms=50)
        empty = 'a'
        while not empty:
            empty = self.ok.read_string(timeout_ms=50)
        return self

    def __exit__(self, *args):
        """Forget PIN, shutdown screen and disconnect."""
        log.info('disconnected from %s', self.device_name)
        self.ok.close()

    def get_identity(self, label, index=0):
        """Parse label string into Identity protobuf."""
        identity = string_to_identity(label)
        identity['proto'] = 'ssh'
        identity['index'] = index
        print 'identity', identity
        return identity


    def get_public_key(self, label):
        log.info('getting public key from %s...', self.device_name)
        log.info('Trying to read the public key...')
        # Compute the challenge pin
        h = hashlib.sha256()
        h.update(label)
        data = h.hexdigest()
        if self.curve == formats.CURVE_NIST256:
            data = '02' + data
        else:
            data = '01'+ data

        data = data.decode("hex")

        log.info('Identity hash =%s', repr(data))
        self.ok.send_message(msg=Message.OKGETPUBKEY, slot_id=132, payload=data)
        time.sleep(.5)
        for _ in xrange(2):
            ok_pubkey = self.ok.read_bytes(64, to_str=True, timeout_ms=10)
            if len(ok_pubkey) == 64:
                break

        log.info('received= %s', repr(ok_pubkey))

        if  len(set(ok_pubkey[34:63])) == 1:
            ok_pubkey = ok_pubkey[0:32]
            log.info('Received Public Key generated by OnlyKey= %s', repr(ok_pubkey))
            vk = ed25519.VerifyingKey(ok_pubkey)
            return formats.export_public_key(vk=vk, label=label)
        else:
            ok_pubkey = ok_pubkey[0:64]
            log.info('Received Public Key generated by OnlyKey= %s', repr(ok_pubkey))
            vk = ecdsa.VerifyingKey.from_string(ok_pubkey, curve=ecdsa.NIST256p)
            return formats.export_public_key(vk=vk, label=label)


    def sign_ssh_challenge(self, label, blob):
        """Sign given blob using a private key, specified by the label."""
        msg = _parse_ssh_blob(blob)
        log.debug('%s: user %r via %r (%r)',
                  msg['conn'], msg['user'], msg['auth'], msg['key_type'])
        log.debug('nonce: %s', binascii.hexlify(msg['nonce']))
        log.debug('fingerprint: %s', msg['public_key']['fingerprint'])
        log.debug('hidden challenge size: %d bytes', len(blob))

        # self.ok.send_large_message(payload=blob, msg=Message.OKSIGNSSHCHALLENGE)
        log.info('please confirm user "%s" login to "%s" using %s',
                 msg['user'], label, self.device_name)

        h1 = hashlib.sha256()
        h1.update(label)
        data = h1.hexdigest()
        data = data.decode("hex")

        test_payload = blob + data
        # Compute the challenge pin
        h2 = hashlib.sha256()
        h2.update(test_payload)
        d = h2.digest()

        assert len(d) == 32

        def get_button(byte):
            ibyte = ord(byte)
            if ibyte < 6:
                return 1
            return ibyte % 5 + 1

        b1, b2, b3 = get_button(d[0]), get_button(d[15]), get_button(d[31])

        log.info('blob to send', repr(test_payload))
        self.ok.send_large_message2(msg=Message.OKSIGNCHALLENGE, payload=test_payload, slot_id=132)

        print 'Please confirm user', msg['user'], 'login to', label, 'using', self.device_name
        print('Enter the 3 digit challenge code shown below on OnlyKey to authenticate')
        print '{} {} {}'.format(b1, b2, b3)
        raw_input()
        for _ in xrange(10):
            result = self.ok.read_bytes(64, to_str=True, timeout_ms=200)
            if len(result) >= 60:
                log.info('received= %s', repr(result))
                while len(result) < 64:
                    result.append(0)
                return result

        raise Exception('failed to sign challenge')

_identity_regexp = re.compile(''.join([
    '^'
    r'(?:(?P<proto>.*)://)?',
    r'(?:(?P<user>.*)@)?',
    r'(?P<host>.*?)',
    r'(?::(?P<port>\w*))?',
    r'(?P<path>/.*)?',
    '$'
]))


def string_to_identity(s, identity_type=dict):
    """Parse string into Identity protobuf."""
    m = _identity_regexp.match(s)
    result = m.groupdict()
    log.debug('parsed identity: %s', result)
    kwargs = {k: v for k, v in result.items() if v}
    return identity_type(**kwargs)


def _parse_ssh_blob(data):
    res = {}
    i = io.BytesIO(data)
    res['nonce'] = util.read_frame(i)
    i.read(1)  # SSH2_MSG_USERAUTH_REQUEST == 50 (from ssh2.h, line 108)
    res['user'] = util.read_frame(i)
    res['conn'] = util.read_frame(i)
    res['auth'] = util.read_frame(i)
    i.read(1)  # have_sig == 1 (from sshconnect2.c, line 1056)
    res['key_type'] = util.read_frame(i)
    public_key = util.read_frame(i)
    res['public_key'] = formats.parse_pubkey(public_key)
    assert not i.read()
    return res
