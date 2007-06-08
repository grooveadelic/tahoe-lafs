#! /usr/bin/env python

from twisted.trial import unittest
from twisted.internet import defer
from twisted.python.failure import Failure
from foolscap import eventual
from allmydata import encode, download
from allmydata.util import bencode
from allmydata.uri import pack_uri
from cStringIO import StringIO

class FakePeer:
    def __init__(self, mode="good"):
        self.ss = FakeStorageServer(mode)

    def callRemote(self, methname, *args, **kwargs):
        def _call():
            meth = getattr(self, methname)
            return meth(*args, **kwargs)
        return defer.maybeDeferred(_call)

    def get_service(self, sname):
        assert sname == "storageserver"
        return self.ss

class FakeStorageServer:
    def __init__(self, mode):
        self.mode = mode
    def callRemote(self, methname, *args, **kwargs):
        def _call():
            meth = getattr(self, methname)
            return meth(*args, **kwargs)
        d = eventual.fireEventually()
        d.addCallback(lambda res: _call())
        return d
    def allocate_buckets(self, verifierid, sharenums, shareize, blocksize, canary):
        if self.mode == "full":
            return (set(), {},)
        elif self.mode == "already got them":
            return (set(sharenums), {},)
        else:
            return (set(), dict([(shnum, FakeBucketWriter(),) for shnum in sharenums]),)

class LostPeerError(Exception):
    pass

class FakeBucketWriter:
    # these are used for both reading and writing
    def __init__(self, mode="good"):
        self.mode = mode
        self.blocks = {}
        self.plaintext_hashes = None
        self.crypttext_hashes = None
        self.block_hashes = None
        self.share_hashes = None
        self.closed = False

    def callRemote(self, methname, *args, **kwargs):
        def _call():
            meth = getattr(self, methname)
            return meth(*args, **kwargs)
        return defer.maybeDeferred(_call)

    def put_block(self, segmentnum, data):
        assert not self.closed
        assert segmentnum not in self.blocks
        if self.mode == "lost" and segmentnum >= 1:
            raise LostPeerError("I'm going away now")
        self.blocks[segmentnum] = data

    def put_plaintext_hashes(self, hashes):
        assert not self.closed
        assert self.plaintext_hashes is None
        self.plaintext_hashes = hashes

    def put_crypttext_hashes(self, hashes):
        assert not self.closed
        assert self.crypttext_hashes is None
        self.crypttext_hashes = hashes

    def put_block_hashes(self, blockhashes):
        assert not self.closed
        assert self.block_hashes is None
        self.block_hashes = blockhashes
        
    def put_share_hashes(self, sharehashes):
        assert not self.closed
        assert self.share_hashes is None
        self.share_hashes = sharehashes

    def put_thingA(self, thingA):
        assert not self.closed
        self.thingA = thingA

    def close(self):
        assert not self.closed
        self.closed = True

    def flip_bit(self, good):
        return good[:-1] + chr(ord(good[-1]) ^ 0x01)

    def get_block(self, blocknum):
        assert isinstance(blocknum, (int, long))
        if self.mode == "bad block":
            return self.flip_bit(self.blocks[blocknum])
        return self.blocks[blocknum]

    def get_plaintext_hashes(self):
        if self.mode == "bad plaintexthash":
            hashes = self.plaintext_hashes[:]
            hashes[1] = self.flip_bit(hashes[1])
            return hashes
        return self.plaintext_hashes
    def get_crypttext_hashes(self):
        if self.mode == "bad crypttexthash":
            hashes = self.crypttext_hashes[:]
            hashes[1] = self.flip_bit(hashes[1])
            return hashes
        return self.crypttext_hashes

    def get_block_hashes(self):
        if self.mode == "bad blockhash":
            hashes = self.block_hashes[:]
            hashes[1] = self.flip_bit(hashes[1])
            return hashes
        return self.block_hashes
    def get_share_hashes(self):
        if self.mode == "bad sharehash":
            hashes = self.share_hashes[:]
            hashes[1] = (hashes[1][0], self.flip_bit(hashes[1][1]))
            return hashes
        if self.mode == "missing sharehash":
            # one sneaky attack would be to pretend we don't know our own
            # sharehash, which could manage to frame someone else.
            # download.py is supposed to guard against this case.
            return []
        return self.share_hashes


def make_data(length):
    data = "happy happy joy joy" * 100
    assert length <= len(data)
    return data[:length]

class Encode(unittest.TestCase):

    def do_encode(self, max_segment_size, datalen, NUM_SHARES, NUM_SEGMENTS,
                  expected_block_hashes, expected_share_hashes):
        data = make_data(datalen)
        # force use of multiple segments
        options = {"max_segment_size": max_segment_size}
        e = encode.Encoder(options)
        nonkey = "\x00" * 16
        e.setup(StringIO(data), nonkey)
        assert e.num_shares == NUM_SHARES # else we'll be completely confused
        e.setup_codec() # need to rebuild the codec for that change
        assert (NUM_SEGMENTS-1)*e.segment_size < len(data) <= NUM_SEGMENTS*e.segment_size
        shareholders = {}
        all_shareholders = []
        for shnum in range(NUM_SHARES):
            peer = FakeBucketWriter()
            shareholders[shnum] = peer
            all_shareholders.append(peer)
        e.set_shareholders(shareholders)
        d = e.start()
        def _check(roothash):
            self.failUnless(isinstance(roothash, str))
            self.failUnlessEqual(len(roothash), 32)
            for i,peer in enumerate(all_shareholders):
                self.failUnless(peer.closed)
                self.failUnlessEqual(len(peer.blocks), NUM_SEGMENTS)
                # each peer gets a full tree of block hashes. For 3 or 4
                # segments, that's 7 hashes. For 5 segments it's 15 hashes.
                self.failUnlessEqual(len(peer.block_hashes),
                                     expected_block_hashes)
                for h in peer.block_hashes:
                    self.failUnlessEqual(len(h), 32)
                # each peer also gets their necessary chain of share hashes.
                # For 100 shares (rounded up to 128 leaves), that's 8 hashes
                self.failUnlessEqual(len(peer.share_hashes),
                                     expected_share_hashes)
                for (hashnum, h) in peer.share_hashes:
                    self.failUnless(isinstance(hashnum, int))
                    self.failUnlessEqual(len(h), 32)
        d.addCallback(_check)

        return d

    # a series of 3*3 tests to check out edge conditions. One axis is how the
    # plaintext is divided into segments: kn+(-1,0,1). Another way to express
    # that is that n%k == -1 or 0 or 1. For example, for 25-byte segments, we
    # might test 74 bytes, 75 bytes, and 76 bytes.

    # on the other axis is how many leaves in the block hash tree we wind up
    # with, relative to a power of 2, so 2^a+(-1,0,1). Each segment turns
    # into a single leaf. So we'd like to check out, e.g., 3 segments, 4
    # segments, and 5 segments.

    # that results in the following series of data lengths:
    #  3 segs: 74, 75, 51
    #  4 segs: 99, 100, 76
    #  5 segs: 124, 125, 101

    # all tests encode to 100 shares, which means the share hash tree will
    # have 128 leaves, which means that buckets will be given an 8-long share
    # hash chain
    
    # all 3-segment files will have a 4-leaf blockhashtree, and thus expect
    # to get 7 blockhashes. 4-segment files will also get 4-leaf block hash
    # trees and 7 blockhashes. 5-segment files will get 8-leaf block hash
    # trees, which get 15 blockhashes.

    def test_send_74(self):
        # 3 segments (25, 25, 24)
        return self.do_encode(25, 74, 100, 3, 7, 8)
    def test_send_75(self):
        # 3 segments (25, 25, 25)
        return self.do_encode(25, 75, 100, 3, 7, 8)
    def test_send_51(self):
        # 3 segments (25, 25, 1)
        return self.do_encode(25, 51, 100, 3, 7, 8)

    def test_send_76(self):
        # encode a 76 byte file (in 4 segments: 25,25,25,1) to 100 shares
        return self.do_encode(25, 76, 100, 4, 7, 8)
    def test_send_99(self):
        # 4 segments: 25,25,25,24
        return self.do_encode(25, 99, 100, 4, 7, 8)
    def test_send_100(self):
        # 4 segments: 25,25,25,25
        return self.do_encode(25, 100, 100, 4, 7, 8)

    def test_send_101(self):
        # encode a 101 byte file (in 5 segments: 25,25,25,25,1) to 100 shares
        return self.do_encode(25, self.make_data(101), 100, 5, 15, 8)

    def test_send_124(self):
        # 5 segments: 25, 25, 25, 25, 24
        return self.do_encode(25, 124, 100, 5, 15, 8)
    def test_send_125(self):
        # 5 segments: 25, 25, 25, 25, 25
        return self.do_encode(25, 125, 100, 5, 15, 8)
    def test_send_101(self):
        # 5 segments: 25, 25, 25, 25, 1
        return self.do_encode(25, 101, 100, 5, 15, 8)

class Roundtrip(unittest.TestCase):
    def send_and_recover(self, k_and_happy_and_n=(25,75,100),
                         AVAILABLE_SHARES=None,
                         datalen=76,
                         max_segment_size=25,
                         bucket_modes={},
                         ):
        NUM_SHARES = k_and_happy_and_n[2]
        if AVAILABLE_SHARES is None:
            AVAILABLE_SHARES = NUM_SHARES
        data = make_data(datalen)
        # force use of multiple segments
        options = {"max_segment_size": max_segment_size,
                   "needed_and_happy_and_total_shares": k_and_happy_and_n}
        e = encode.Encoder(options)
        nonkey = "\x00" * 16
        e.setup(StringIO(data), nonkey)

        assert e.num_shares == NUM_SHARES # else we'll be completely confused
        e.setup_codec() # need to rebuild the codec for that change

        shareholders = {}
        all_peers = []
        for shnum in range(NUM_SHARES):
            mode = bucket_modes.get(shnum, "good")
            peer = FakeBucketWriter(mode)
            shareholders[shnum] = peer
        e.set_shareholders(shareholders)
        e.set_thingA_data({'verifierid': "V" * 20,
                           'fileid': "F" * 20,
                           })
        d = e.start()
        d.addCallback(self.recover, nonkey, e, shareholders, AVAILABLE_SHARES)
        def _downloaded(newdata):
            self.failUnless(newdata == data)
        d.addCallback(_downloaded)
        return d

    def recover(self, thingA_hash, nonkey, e, shareholders, AVAILABLE_SHARES):
        URI = pack_uri(storage_index="S" * 20,
                       key=nonkey,
                       thingA_hash=thingA_hash,
                       needed_shares=e.required_shares,
                       total_shares=e.num_shares,
                       size=e.file_size)
        client = None
        target = download.Data()
        fd = download.FileDownloader(client, URI, target)
        fd.check_verifierid = False
        fd.check_fileid = False
        # grab a copy of thingA from one of the shareholders
        thingA = shareholders[0].thingA
        thingA_data = bencode.bdecode(thingA)
        NOTthingA = {'codec_name': e._codec.get_encoder_type(),
                  'codec_params': e._codec.get_serialized_params(),
                  'tail_codec_params': e._tail_codec.get_serialized_params(),
                  'verifierid': "V" * 20,
                  'fileid': "F" * 20,
                     #'share_root_hash': roothash,
                  'segment_size': e.segment_size,
                  'needed_shares': e.required_shares,
                  'total_shares': e.num_shares,
                  }
        fd._got_thingA(thingA_data)
        for shnum, bucket in shareholders.items():
            if shnum < AVAILABLE_SHARES and bucket.closed:
                fd.add_share_bucket(shnum, bucket)
        fd._got_all_shareholders(None)
        fd._create_validated_buckets(None)
        d = fd._download_all_segments(None)
        d.addCallback(fd._done)
        return d

    def test_not_enough_shares(self):
        d = self.send_and_recover((4,8,10), AVAILABLE_SHARES=2)
        def _done(res):
            self.failUnless(isinstance(res, Failure))
            self.failUnless(res.check(download.NotEnoughPeersError))
        d.addBoth(_done)
        return d

    def test_one_share_per_peer(self):
        return self.send_and_recover()

    def test_74(self):
        return self.send_and_recover(datalen=74)
    def test_75(self):
        return self.send_and_recover(datalen=75)
    def test_51(self):
        return self.send_and_recover(datalen=51)

    def test_99(self):
        return self.send_and_recover(datalen=99)
    def test_100(self):
        return self.send_and_recover(datalen=100)
    def test_76(self):
        return self.send_and_recover(datalen=76)

    def test_124(self):
        return self.send_and_recover(datalen=124)
    def test_125(self):
        return self.send_and_recover(datalen=125)
    def test_101(self):
        return self.send_and_recover(datalen=101)

    # the following tests all use 4-out-of-10 encoding

    def test_bad_blocks(self):
        # the first 6 servers have bad blocks, which will be caught by the
        # blockhashes
        modemap = dict([(i, "bad block")
                        for i in range(6)]
                       + [(i, "good")
                          for i in range(6, 10)])
        return self.send_and_recover((4,8,10), bucket_modes=modemap)

    def test_bad_blocks_failure(self):
        # the first 7 servers have bad blocks, which will be caught by the
        # blockhashes, and the download will fail
        modemap = dict([(i, "bad block")
                        for i in range(7)]
                       + [(i, "good")
                          for i in range(7, 10)])
        d = self.send_and_recover((4,8,10), bucket_modes=modemap)
        def _done(res):
            self.failUnless(isinstance(res, Failure))
            self.failUnless(res.check(download.NotEnoughPeersError))
        d.addBoth(_done)
        return d

    def test_bad_blockhashes(self):
        # the first 6 servers have bad block hashes, so the blockhash tree
        # will not validate
        modemap = dict([(i, "bad blockhash")
                        for i in range(6)]
                       + [(i, "good")
                          for i in range(6, 10)])
        return self.send_and_recover((4,8,10), bucket_modes=modemap)

    def test_bad_blockhashes_failure(self):
        # the first 7 servers have bad block hashes, so the blockhash tree
        # will not validate, and the download will fail
        modemap = dict([(i, "bad blockhash")
                        for i in range(7)]
                       + [(i, "good")
                          for i in range(7, 10)])
        d = self.send_and_recover((4,8,10), bucket_modes=modemap)
        def _done(res):
            self.failUnless(isinstance(res, Failure))
            self.failUnless(res.check(download.NotEnoughPeersError))
        d.addBoth(_done)
        return d

    def test_bad_sharehashes(self):
        # the first 6 servers have bad block hashes, so the sharehash tree
        # will not validate
        modemap = dict([(i, "bad sharehash")
                        for i in range(6)]
                       + [(i, "good")
                          for i in range(6, 10)])
        return self.send_and_recover((4,8,10), bucket_modes=modemap)

    def test_bad_sharehashes_failure(self):
        # the first 7 servers have bad block hashes, so the sharehash tree
        # will not validate, and the download will fail
        modemap = dict([(i, "bad sharehash")
                        for i in range(7)]
                       + [(i, "good")
                          for i in range(7, 10)])
        d = self.send_and_recover((4,8,10), bucket_modes=modemap)
        def _done(res):
            self.failUnless(isinstance(res, Failure))
            self.failUnless(res.check(download.NotEnoughPeersError))
        d.addBoth(_done)
        return d

    def test_missing_sharehashes(self):
        # the first 6 servers are missing their sharehashes, so the
        # sharehash tree will not validate
        modemap = dict([(i, "missing sharehash")
                        for i in range(6)]
                       + [(i, "good")
                          for i in range(6, 10)])
        return self.send_and_recover((4,8,10), bucket_modes=modemap)

    def test_missing_sharehashes_failure(self):
        # the first 7 servers are missing their sharehashes, so the
        # sharehash tree will not validate, and the download will fail
        modemap = dict([(i, "missing sharehash")
                        for i in range(7)]
                       + [(i, "good")
                          for i in range(7, 10)])
        d = self.send_and_recover((4,8,10), bucket_modes=modemap)
        def _done(res):
            self.failUnless(isinstance(res, Failure))
            self.failUnless(res.check(download.NotEnoughPeersError))
        d.addBoth(_done)
        return d

    def test_lost_one_shareholder(self):
        # we have enough shareholders when we start, but one segment in we
        # lose one of them. The upload should still succeed, as long as we
        # still have 'shares_of_happiness' peers left.
        modemap = dict([(i, "good") for i in range(9)] +
                       [(i, "lost") for i in range(9, 10)])
        return self.send_and_recover((4,8,10), bucket_modes=modemap)

    def test_lost_many_shareholders(self):
        # we have enough shareholders when we start, but one segment in we
        # lose all but one of them. The upload should fail.
        modemap = dict([(i, "good") for i in range(1)] +
                       [(i, "lost") for i in range(1, 10)])
        d = self.send_and_recover((4,8,10), bucket_modes=modemap)
        def _done(res):
            self.failUnless(isinstance(res, Failure))
            self.failUnless(res.check(encode.NotEnoughPeersError))
        d.addBoth(_done)
        return d

    def test_lost_all_shareholders(self):
        # we have enough shareholders when we start, but one segment in we
        # lose all of them. The upload should fail.
        modemap = dict([(i, "lost") for i in range(10)])
        d = self.send_and_recover((4,8,10), bucket_modes=modemap)
        def _done(res):
            self.failUnless(isinstance(res, Failure))
            self.failUnless(res.check(encode.NotEnoughPeersError))
        d.addBoth(_done)
        return d

