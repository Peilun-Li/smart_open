#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# Copyright (C) 2015 Radim Rehurek <me@radimrehurek.com>
#
# This code is distributed under the terms and conditions
# from the MIT License (MIT).


"""
Utilities for streaming from several file-like data storages: S3 / HDFS / standard
filesystem / compressed files..., using a single, Pythonic API.

The streaming makes heavy use of generators and pipes, to avoid loading
full file contents into memory, allowing work with arbitrarily large files.

The main methods are:

* `smart_open()`, which opens the given file for reading/writing
* `s3_iter_bucket()`, which goes over all keys in an S3 bucket in parallel

"""

import logging
import os
import subprocess
import sys
import requests
import io


IS_PY2 = (sys.version_info[0] == 2)

if IS_PY2:
    import cStringIO as StringIO
    import httplib
elif sys.version_info[0] == 3:
    import io as StringIO
    import http.client as httplib

from boto.compat import BytesIO, urlsplit, six
import boto.s3.connection
import boto.s3.key
from ssl import SSLError

logger = logging.getLogger(__name__)

# Multiprocessing is unavailable in App Engine (and possibly other sandboxes).
# The only method currently relying on it is s3_iter_bucket, which is instructed
# whether to use it by the MULTIPROCESSING flag.
MULTIPROCESSING = False
try:
    import multiprocessing.pool
    MULTIPROCESSING = True
except ImportError:
    logger.warning("multiprocessing could not be imported and won't be used")
    from itertools import imap

from . import gzipstreamfile


S3_MIN_PART_SIZE = 50 * 1024**2  # minimum part size for S3 multipart uploads
WEBHDFS_MIN_PART_SIZE = 50 * 1024**2  # minimum part size for HDFS multipart uploads


def smart_open(uri, mode="rb", **kw):
    """
    Open the given S3 / HDFS / filesystem file pointed to by `uri` for reading or writing.

    The only supported modes for now are 'rb' (read, default) and 'wb' (replace & write).

    The reads/writes are memory efficient (streamed) and therefore suitable for
    arbitrarily large files.

    The `uri` can be either:

    1. a URI for the local filesystem (compressed ``.gz`` or ``.bz2`` files handled automatically):
       `./lines.txt`, `/home/joe/lines.txt.gz`, `file:///home/joe/lines.txt.bz2`
    2. a URI for HDFS: `hdfs:///some/path/lines.txt`
    3. a URI for Amazon's S3 (can also supply credentials inside the URI):
       `s3://my_bucket/lines.txt`, `s3://my_aws_key_id:key_secret@my_bucket/lines.txt`
    4. an instance of the boto.s3.key.Key class.

    Examples::

      >>> # stream lines from http; you can use context managers too:
      >>> with smart_open.smart_open('http://www.google.com') as fin:
      ...     for line in fin:
      ...         print line

      >>> # stream lines from S3; you can use context managers too:
      >>> with smart_open.smart_open('s3://mybucket/mykey.txt') as fin:
      ...     for line in fin:
      ...         print line

      >>> # you can also use a boto.s3.key.Key instance directly:
      >>> key = boto.connect_s3().get_bucket("my_bucket").get_key("my_key")
      >>> with smart_open.smart_open(key) as fin:
      ...     for line in fin:
      ...         print line

      >>> # stream line-by-line from an HDFS file
      >>> for line in smart_open.smart_open('hdfs:///user/hadoop/my_file.txt'):
      ...    print line

      >>> # stream content *into* S3:
      >>> with smart_open.smart_open('s3://mybucket/mykey.txt', 'wb') as fout:
      ...     for line in ['first line', 'second line', 'third line']:
      ...          fout.write(line + '\n')

      >>> # stream from/to (compressed) local files:
      >>> for line in smart_open.smart_open('/home/radim/my_file.txt'):
      ...    print line
      >>> for line in smart_open.smart_open('/home/radim/my_file.txt.gz'):
      ...    print line
      >>> with smart_open.smart_open('/home/radim/my_file.txt.gz', 'wb') as fout:
      ...    fout.write("hello world!\n")
      >>> with smart_open.smart_open('/home/radim/another.txt.bz2', 'wb') as fout:
      ...    fout.write("good bye!\n")
      >>> # stream from/to (compressed) local files with Expand ~ and ~user constructions:
      >>> for line in smart_open.smart_open('~/my_file.txt'):
      ...    print line
      >>> for line in smart_open.smart_open('my_file.txt'):
      ...    print line

    """

    # validate mode parameter
    if not isinstance(mode, six.string_types):
        raise TypeError('mode should be a string')

    if isinstance(uri, six.string_types):
        # this method just routes the request to classes handling the specific storage
        # schemes, depending on the URI protocol in `uri`
        parsed_uri = ParseUri(uri)

        if parsed_uri.scheme in ("file", ):
            # local files -- both read & write supported
            # compression, if any, is determined by the filename extension (.gz, .bz2)
            return file_smart_open(parsed_uri.uri_path, mode)
        elif parsed_uri.scheme in ("s3", "s3n", "s3u"):
            kwargs = {}
            # Get an S3 host. It is required for sigv4 operations.
            host = kw.pop('host', parsed_uri.host)
            port = kw.pop('port', parsed_uri.port)
            if port != 443:
                kwargs['port'] = port

            if not kw.pop('is_secure', parsed_uri.scheme != 's3u'):
                kwargs['is_secure'] = False
                # If the security model docker is overridden, honor the host directly.
                kwargs['calling_format'] = boto.s3.connection.OrdinaryCallingFormat()

            # For credential order of precedence see
            # http://boto.cloudhackers.com/en/latest/boto_config_tut.html#credentials
            s3_connection = boto.connect_s3(
                aws_access_key_id=parsed_uri.access_id,
                host=host,
                aws_secret_access_key=parsed_uri.access_secret,
                profile_name=kw.pop('profile_name', None),
                **kwargs)

            bucket = s3_connection.get_bucket(parsed_uri.bucket_id)
            if mode in ('r', 'rb'):
                key = bucket.get_key(parsed_uri.key_id)
                if key is None:
                    raise KeyError(parsed_uri.key_id)
                return S3OpenRead(key)
            elif mode in ('w', 'wb'):
                key = bucket.get_key(parsed_uri.key_id, validate=False)
                if key is None:
                    raise KeyError(parsed_uri.key_id)
                return S3OpenWrite(key, **kw)
            else:
                raise NotImplementedError("file mode %s not supported for %r scheme", mode, parsed_uri.scheme)

        elif parsed_uri.scheme in ("hdfs", ):
            if mode in ('r', 'rb'):
                return HdfsOpenRead(parsed_uri, **kw)
            if mode in ('w', 'wb'):
                return HdfsOpenWrite(parsed_uri, **kw)
            else:
                raise NotImplementedError("file mode %s not supported for %r scheme", mode, parsed_uri.scheme)
        elif parsed_uri.scheme in ("webhdfs", ):
            if mode in ('r', 'rb'):
                return WebHdfsOpenRead(parsed_uri, **kw)
            elif mode in ('w', 'wb'):
                return WebHdfsOpenWrite(parsed_uri, **kw)
            else:
                raise NotImplementedError("file mode %s not supported for %r scheme", mode, parsed_uri.scheme)
        elif parsed_uri.scheme.startswith('http'):
            if mode in ('r', 'rb'):
                return HttpOpenRead(parsed_uri, **kw)
            else:
                raise NotImplementedError("file mode %s not supported for %r scheme", mode, parsed_uri.scheme)
        else:
            raise NotImplementedError("scheme %r is not supported", parsed_uri.scheme)
    elif isinstance(uri, boto.s3.key.Key):
        # handle case where we are given an S3 key directly
        if mode in ('r', 'rb'):
            return S3OpenRead(uri)
        elif mode in ('w', 'wb'):
            return S3OpenWrite(uri, **kw)
    elif hasattr(uri, 'read'):
        # simply pass-through if already a file-like
        return uri
    else:
        raise TypeError('don\'t know how to handle uri %s' % repr(uri))


class ParseUri(object):
    """
    Parse the given URI.

    Supported URI schemes are "file", "s3", "s3n", "s3u" and "hdfs".

      * s3 and s3n are treated the same way.
      * s3u is s3 but without SSL.

    Valid URI examples::

      * s3://my_bucket/my_key
      * s3://my_key:my_secret@my_bucket/my_key
      * s3://my_key:my_secret@my_server:my_port@my_bucket/my_key
      * hdfs:///path/file
      * hdfs://path/file
      * webhdfs://host:port/path/file
      * ./local/path/file
      * ~/local/path/file
      * local/path/file
      * ./local/path/file.gz
      * file:///home/user/file
      * file:///home/user/file.bz2

    """
    def __init__(self, uri, default_scheme="file"):
        """
        Assume `default_scheme` if no scheme given in `uri`.

        """
        if os.name == 'nt':
            # urlsplit doesn't work on Windows -- it parses the drive as the scheme...
            if '://' not in uri:
                # no protocol given => assume a local file
                uri = 'file://' + uri
        parsed_uri = urlsplit(uri, allow_fragments=False)
        self.scheme = parsed_uri.scheme if parsed_uri.scheme else default_scheme

        if self.scheme == "hdfs":
            self.uri_path = parsed_uri.netloc + parsed_uri.path
            self.uri_path = "/" + self.uri_path.lstrip("/")

            if not self.uri_path:
                raise RuntimeError("invalid HDFS URI: %s" % uri)
        elif self.scheme == "webhdfs":
            self.uri_path = parsed_uri.netloc + "/webhdfs/v1" + parsed_uri.path
            if parsed_uri.query:
                self.uri_path += "?" + parsed_uri.query

            if not self.uri_path:
                raise RuntimeError("invalid WebHDFS URI: %s" % uri)
        elif self.scheme in ("s3", "s3n", "s3u"):
            self.bucket_id = (parsed_uri.netloc + parsed_uri.path).split('@')
            self.key_id = None
            self.port = 443
            self.host = boto.config.get('s3', 'host', 's3.amazonaws.com')
            self.ordinary_calling_format = False
            if len(self.bucket_id) == 1:
                # URI without credentials: s3://bucket/object
                self.bucket_id, self.key_id = self.bucket_id[0].split('/', 1)
                # "None" credentials are interpreted as "look for credentials in other locations" by boto
                self.access_id, self.access_secret = None, None
            elif len(self.bucket_id) == 2 and len(self.bucket_id[0].split(':')) == 2:
                # URI in full format: s3://key:secret@bucket/object
                # access key id: [A-Z0-9]{20}
                # secret access key: [A-Za-z0-9/+=]{40}
                acc, self.bucket_id = self.bucket_id
                self.access_id, self.access_secret = acc.split(':')
                self.bucket_id, self.key_id = self.bucket_id.split('/', 1)
            elif len(self.bucket_id) == 3 and len(self.bucket_id[0].split(':')) == 2:
                # or URI in extended format: s3://key:secret@server[:port]@bucket/object
                acc,  server, self.bucket_id = self.bucket_id
                self.access_id, self.access_secret = acc.split(':')
                self.bucket_id, self.key_id = self.bucket_id.split('/', 1)
                server = server.split(':')
                self.ordinary_calling_format = True
                self.host = server[0]
                if len(server) == 2:
                    self.port = int(server[1])
            else:
                # more than 2 '@' means invalid uri
                # Bucket names must be at least 3 and no more than 63 characters long.
                # Bucket names must be a series of one or more labels.
                # Adjacent labels are separated by a single period (.).
                # Bucket names can contain lowercase letters, numbers, and hyphens.
                # Each label must start and end with a lowercase letter or a number.
                raise RuntimeError("invalid S3 URI: %s" % uri)
        elif self.scheme == 'file':
            self.uri_path = parsed_uri.netloc + parsed_uri.path

            # '~/tmp' may be expanded to '/Users/username/tmp'
            self.uri_path = os.path.expanduser(self.uri_path)

            if not self.uri_path:
                raise RuntimeError("invalid file URI: %s" % uri)
        elif self.scheme.startswith('http'):
            self.uri_path = uri
        else:
            raise NotImplementedError("unknown URI scheme %r in %r" % (self.scheme, uri))


def is_gzip(name):
    """Return True if the name indicates that the file is compressed with
    gzip."""
    return name.endswith(".gz")


class S3ReadStreamInner(object):

    def __init__(self, stream):
        self.stream = stream
        self.unused_buffer = b''
        self.closed = False
        self.finished = False

    def read_until_eof(self):
        #
        # This method is here because boto.s3.Key.read() reads the entire
        # file, which isn't expected behavior.
        #
        # https://github.com/boto/boto/issues/3311
        #
        buf = b""
        while not self.finished:
            raw = self.stream.read(io.DEFAULT_BUFFER_SIZE)
            if len(raw) > 0:
                buf += raw
            else:
                self.finished = True
        return buf

    def read_from_buffer(self, size):
        """Remove at most size bytes from our buffer and return them."""
        part = self.unused_buffer[:size]
        self.unused_buffer = self.unused_buffer[size:]
        return part

    def read(self, size=None):
        if not size or size < 0:
            return self.read_from_buffer(
                len(self.unused_buffer)) + self.read_until_eof()

        # Use unused data first
        if len(self.unused_buffer) >= size:
            return self.read_from_buffer(size)

        # If the stream is finished and no unused raw data, return what we have
        if self.stream.closed or self.finished:
            self.finished = True
            return self.read_from_buffer(size)

        # Consume new data in chunks and return it.
        while len(self.unused_buffer) < size:
            raw = self.stream.read(io.DEFAULT_BUFFER_SIZE)
            if len(raw):
                self.unused_buffer += raw
            else:
                self.finished = True
                break

        return self.read_from_buffer(size)

    def readinto(self, b):
        # Read up to len(b) bytes into bytearray b
        # Sadly not as efficient as lower level
        data = self.read(len(b))
        if not data:
            return None
        b[:len(data)] = data
        return len(data)

    def readable(self):
        # io.BufferedReader needs us to appear readable
        return True

    def _checkReadable(self, msg=None):
        # This is required to satisfy io.BufferedReader on Python 2.6.
        # Another way to achieve this is to inherit from io.IOBase, but that
        # leads to other problems.
        return True


class S3ReadStream(io.BufferedReader):

    def __init__(self, key):
        self.stream = S3ReadStreamInner(key)
        super(S3ReadStream, self).__init__(self.stream)

    def read(self, *args, **kwargs):
        # Patch read to return '' instead of raise Value Error
        # TODO: what actually raises ValueError in the following code?
        try:
            #
            # io.BufferedReader behaves differently to a built-in file object.
            # If the object is in non-blocking mode and no bytes are available,
            # the former will return None. The latter returns an empty string.
            # We want to behave like a built-in file object here.
            #
            result = super(S3ReadStream, self).read(*args, **kwargs)
            if result is None:
                return ""
            return result
        except ValueError:
            return ''

    def readline(self, *args, **kwargs):
        # Patch readline to return '' instead of raise Value Error
        # TODO: what actually raises ValueError in the following code?
        try:
            result = super(S3ReadStream, self).readline(*args, **kwargs)
            return result
        except ValueError:
            return ''


class S3OpenRead(object):
    """
    Implement streamed reader from S3, as an iterable & context manager.

    Supports reading from gzip-compressed files.  Identifies such files by
    their extension.

    """
    def __init__(self, read_key):
        if not hasattr(read_key, "bucket") and not hasattr(read_key, "name") and not hasattr(read_key, "read") \
                and not hasattr(read_key, "close"):
            raise TypeError("can only process S3 keys")
        self.read_key = read_key
        self._open_reader()

    def _open_reader(self):
        if is_gzip(self.read_key.name):
            self.reader = gzipstreamfile.GzipStreamFile(self.read_key)
        else:
            self.reader = S3ReadStream(self.read_key)

    def __iter__(self):
        for line in self.reader:
            yield line

    def readline(self):
        return self.reader.readline()

    def read(self, size=None):
        """
        Read a specified number of bytes from the key.

        """
        return self.reader.read(size)

    def seek(self, offset, whence=0):
        """
        Seek to the specified position.

        Only seeking to the beginning (offset=0) supported for now.

        """
        if whence != 0 or offset != 0:
            raise NotImplementedError("seek other than offset=0 not implemented yet")
        self.read_key.close(fast=True)
        self._open_reader()

    def __enter__(self):
        return self

    def __exit__(self, type, value, traceback):
        self.read_key.close(fast=True)

    def __str__(self):
        return "%s<key: %s>" % (
            self.__class__.__name__, self.read_key
        )



class HdfsOpenRead(object):
    """
    Implement streamed reader from HDFS, as an iterable & context manager.

    """
    def __init__(self, parsed_uri):
        if parsed_uri.scheme not in ("hdfs"):
            raise TypeError("can only process HDFS files")
        self.parsed_uri = parsed_uri

    def __iter__(self):
        hdfs = subprocess.Popen(["hdfs", "dfs", "-cat", self.parsed_uri.uri_path], stdout=subprocess.PIPE)
        return hdfs.stdout

    def read(self, size=None):
        raise NotImplementedError("read() not implemented yet")

    def seek(self, offset, whence=None):
        raise NotImplementedError("seek() not implemented yet")

    def __enter__(self):
        return self

    def __exit__(self, type, value, traceback):
        pass


class HdfsOpenWrite(object):
    """
    Implement streamed writer from HDFS, as an iterable & context manager.

    """
    def __init__(self, parsed_uri):
        if parsed_uri.scheme not in ("hdfs"):
            raise TypeError("can only process HDFS files")
        self.parsed_uri = parsed_uri
        self.out_pipe = subprocess.Popen(["hdfs","dfs","-put","-f","-",self.parsed_uri.uri_path], stdin=subprocess.PIPE)

    def write(self, b):
        self.out_pipe.stdin.write(b)

    def seek(self, offset, whence=None):
        raise NotImplementedError("seek() not implemented yet")

    def __enter__(self):
        return self

    def close(self):
        self.out_pipe.stdin.close()

    def __exit__(self, type, value, traceback):
        self.close()


class WebHdfsOpenRead(object):
    """
    Implement streamed reader from WebHDFS, as an iterable & context manager.
    NOTE: it does not support kerberos authentication yet

    """
    def __init__(self, parsed_uri):
        if parsed_uri.scheme not in ("webhdfs"):
            raise TypeError("can only process WebHDFS files")
        self.parsed_uri = parsed_uri
        self.offset = 0

    def __iter__(self):
        payload = {"op": "OPEN"}
        response = requests.get("http://" + self.parsed_uri.uri_path, params=payload, stream=True)
        return response.iter_lines()

    def read(self, size=None):
        """
        Read the specific number of bytes from the file

        Note read() and line iteration (`for line in self: ...`) each have their
        own file position, so they are independent. Doing a `read` will not affect
        the line iteration, and vice versa.
        """
        if not size or size < 0:
            payload = {"op": "OPEN", "offset": self.offset}
            self.offset = 0
        else:
            payload = {"op": "OPEN", "offset": self.offset, "length": size}
            self.offset = self.offset + size
        response = requests.get("http://" + self.parsed_uri.uri_path, params=payload, stream=True)
        return response.content

    def seek(self, offset, whence=0):
        """
        Seek to the specified position.

        Only seeking to the beginning (offset=0) supported for now.

        """
        if whence == 0 and offset == 0:
            self.offset = 0
        elif whence == 0:
            self.offset = offset
        else:
            raise NotImplementedError("operations with whence not implemented yet")

    def __enter__(self):
        return self

    def __exit__(self, type, value, traceback):
        pass


def make_closing(base, **attrs):
    """
    Add support for `with Base(attrs) as fout:` to the base class if it's missing.
    The base class' `close()` method will be called on context exit, to always close the file properly.

    This is needed for gzip.GzipFile, bz2.BZ2File etc in older Pythons (<=2.6), which otherwise
    raise "AttributeError: GzipFile instance has no attribute '__exit__'".

    """
    if not hasattr(base, '__enter__'):
        attrs['__enter__'] = lambda self: self
    if not hasattr(base, '__exit__'):
        attrs['__exit__'] = lambda self, type, value, traceback: self.close()
    return type('Closing' + base.__name__, (base, object), attrs)


def compression_wrapper(file_obj, filename, mode):
    """
    This function will wrap the file_obj with an appropriate
    [de]compression mechanism based on the extension of the filename.

    file_obj must either be a filehandle object, or a class which behaves
        like one.

    If the filename extension isn't recognized, will simply return the original
    file_obj.
    """
    _, ext = os.path.splitext(filename)
    if ext == '.bz2':
        if IS_PY2:
            from bz2file import BZ2File
        else:
            from bz2 import BZ2File
        return make_closing(BZ2File)(file_obj, mode)

    elif ext == '.gz':
        from gzip import GzipFile
        return make_closing(GzipFile)(fileobj=file_obj, mode=mode)

    else:
        return file_obj


def file_smart_open(fname, mode='rb'):
    """
    Stream from/to local filesystem, transparently (de)compressing gzip and bz2
    files if necessary.

    """
    return compression_wrapper(open(fname, mode), fname, mode)


class HttpReadStream(object):
    """
    Implement streamed reader from a web site, as an iterable & context manager.
    Supports Kerberos and Basic HTTP authentication.

    As long as you don't mix different access patterns (readline vs readlines vs
    read(n) vs read() vs iteration) this will load efficiently in memory.

    """
    def __init__(self, url, mode='r', kerberos=False, user=None, password=None):
        """
        If Kerberos is True, will attempt to use the local Kerberos credentials.
        Otherwise, will try to use "basic" HTTP authentication via username/password.

        If none of those are set, will connect unauthenticated.
        """
        if kerberos:
            import requests_kerberos
            auth = requests_kerberos.HTTPKerberosAuth()
        elif user is not None and password is not None:
            auth = (user, password)
        else:
            auth = None
        
        self.response = requests.get(url, auth=auth, stream=True)

        if not self.response.ok:
            self.response.raise_for_status()

        self.mode = mode
        self._read_buffer = None
        self._read_iter = None
        self._readline_iter = None

    def __iter__(self):
        return self.response.iter_lines()

    def binary_content(self):
        """Return the content of the request as bytes."""
        return self.response.content

    def readline(self):
        """
        Mimics the readline call to a filehandle object.
        """
        if self._readline_iter is None:
            self._readline_iter = self.response.iter_lines()

        try:
            return next(self._readline_iter)
        except StopIteration:
            # When readline runs out of data, it just returns an empty string
            return ''

    def readlines(self):
        """
        Mimics the readlines call to a filehandle object.
        """
        return list(self.response.iter_lines())

    def seek(self):
        raise NotImplementedError('seek() is not implemented')

    def read(self, size=None):
        """
        Mimics the read call to a filehandle object.
        """
        if size is None:
            return self.response.content
        else:
            if self._read_iter is None:
                self._read_iter = self.response.iter_content(size)
                self._read_buffer = next(self._read_iter)
            
            while len(self._read_buffer) < size:
                try:
                    self._read_buffer += next(self._read_iter)
                except StopIteration:
                    # Oops, ran out of data early.
                    retval = self._read_buffer
                    self._read_buffer = ''
                    if len(retval) == 0:
                        # When read runs out of data, it just returns empty
                        return ''
                    else:
                        return retval
            
            # If we got here, it means we have enough data in the buffer
            # to return to the caller.
            retval = self._read_buffer[:size]
            self._read_buffer = self._read_buffer[size:]
            return retval

    def __enter__(self, *args, **kwargs):
        return self

    def __exit__(self, *args, **kwargs):
        self.response.close()


def HttpOpenRead(parsed_uri, mode='r', **kwargs):
    if parsed_uri.scheme not in ('http', 'https'):
        raise TypeError("can only process http/https urls")
    if mode not in ('r', 'rb'):
        raise NotImplementedError('Streaming write to http not supported')

    url = parsed_uri.uri_path

    response = HttpReadStream(url, **kwargs)

    fname = urlsplit(url, allow_fragments=False).path.split('/')[-1]

    if fname.endswith('.gz'):
        #  Gzip needs a seek-able filehandle, so we need to buffer it.
        buffer = make_closing(io.BytesIO)(response.binary_content())
        return compression_wrapper(buffer, fname, mode)
    else:
        return compression_wrapper(response, fname, mode)


class S3OpenWrite(object):
    """
    Context manager for writing into S3 files.

    """
    def __init__(self, outkey, min_part_size=S3_MIN_PART_SIZE, **kw):
        """
        Streamed input is uploaded in chunks, as soon as `min_part_size` bytes are
        accumulated (50MB by default). The minimum chunk size allowed by AWS S3
        is 5MB.

        """
        if not hasattr(outkey, "bucket") and not hasattr(outkey, "name"):
            raise TypeError("can only process S3 keys")

        # if is_gzip(outkey.name):
        #    raise NotImplementedError("streaming write to S3 gzip not supported")

        self.outkey = outkey
        self.min_part_size = min_part_size

        if min_part_size < 5 * 1024 ** 2:
            logger.warning("S3 requires minimum part size >= 5MB; multipart upload may fail")

        # initialize mulitpart upload
        self.mp = self.outkey.bucket.initiate_multipart_upload(self.outkey, **kw)

        # initialize stats
        self.lines = []
        self.total_size = 0
        self.chunk_bytes = 0
        self.parts = 0

    def __str__(self):
        return "%s<key: %s, min_part_size: %s>" % (
            self.__class__.__name__, self.outkey, self.min_part_size,
            )

    def write(self, b):
        """
        Write the given bytes (binary string) into the S3 file from constructor.

        Note there's buffering happening under the covers, so this may not actually
        do any HTTP transfer right away.

        """
        if isinstance(b, six.text_type):
            # not part of API: also accept unicode => encode it as utf8
            b = b.encode('utf8')

        if not isinstance(b, six.binary_type):
            raise TypeError("input must be a binary string")

        self.lines.append(b)
        self.chunk_bytes += len(b)
        self.total_size += len(b)

        if self.chunk_bytes >= self.min_part_size:
            buff = b"".join(self.lines)
            logger.info("uploading part #%i, %i bytes (total %.3fGB)" % (self.parts, len(buff), self.total_size / 1024.0 ** 3))
            self.mp.upload_part_from_file(BytesIO(buff), part_num=self.parts + 1)
            logger.debug("upload of part #%i finished" % self.parts)
            self.parts += 1
            self.lines, self.chunk_bytes = [], 0

    def seek(self, offset, whence=None):
        raise NotImplementedError("seek() not implemented yet")

    def close(self):
        buff = b"".join(self.lines)
        if buff:
            logger.info("uploading last part #%i, %i bytes (total %.3fGB)" % (self.parts, len(buff), self.total_size / 1024.0 ** 3))
            self.mp.upload_part_from_file(BytesIO(buff), part_num=self.parts + 1)
            logger.debug("upload of last part #%i finished" % self.parts)

        if self.total_size:
            self.mp.complete_upload()
        else:
            # AWS complains with "The XML you provided was not well-formed or did not validate against our published schema"
            # when the input is completely empty => abort the upload, no file created
            logger.info("empty input, ignoring multipart upload")
            self.outkey.bucket.cancel_multipart_upload(self.mp.key_name, self.mp.id)
            # So, instead, create an empty file like this
            logger.info("setting an empty value for the key")
            self.outkey.set_contents_from_string('')

    def __enter__(self):
        return self

    def _termination_error(self):
        logger.exception("encountered error while terminating multipart upload; attempting cancel")
        self.outkey.bucket.cancel_multipart_upload(self.mp.key_name, self.mp.id)
        logger.info("cancel completed")

    def __exit__(self, type, value, traceback):
        if type is not None:
            self._termination_error()
            return False

        try:
            self.close()
        except:
            self._termination_error()
            raise


class WebHdfsOpenWrite(object):
    """
    Context manager for writing into webhdfs files

    """
    def __init__(self, parsed_uri, min_part_size=WEBHDFS_MIN_PART_SIZE):
        if parsed_uri.scheme not in ("webhdfs"):
            raise TypeError("can only process WebHDFS files")
        self.parsed_uri = parsed_uri
        self.closed = False
        self.min_part_size = min_part_size
        # creating empty file first
        payload = {"op": "CREATE", "overwrite": True}
        init_response = requests.put("http://" + self.parsed_uri.uri_path, params=payload, allow_redirects=False)
        if not init_response.status_code == httplib.TEMPORARY_REDIRECT:
            raise WebHdfsException(str(init_response.status_code) + "\n" + init_response.content)
        uri = init_response.headers['location']
        response = requests.put(uri, data="", headers={'content-type': 'application/octet-stream'})
        if not response.status_code == httplib.CREATED:
            raise WebHdfsException(str(response.status_code) + "\n" + response.content)
        self.lines = []
        self.parts = 0
        self.chunk_bytes = 0
        self.total_size = 0

    def upload(self, data):
        payload = {"op": "APPEND"}
        init_response = requests.post("http://" + self.parsed_uri.uri_path, params=payload, allow_redirects=False)
        if not init_response.status_code == httplib.TEMPORARY_REDIRECT:
            raise WebHdfsException(str(init_response.status_code) + "\n" + init_response.content)
        uri = init_response.headers['location']
        response = requests.post(uri, data=data, headers={'content-type': 'application/octet-stream'})
        if not response.status_code == httplib.OK:
            raise WebHdfsException(str(response.status_code) + "\n" + response.content)

    def write(self, b):
        """
        Write the given bytes (binary string) into the WebHDFS file from constructor.

        """
        if self.closed:
            raise ValueError("I/O operation on closed file")
        if isinstance(b, six.text_type):
            # not part of API: also accept unicode => encode it as utf8
            b = b.encode('utf8')

        if not isinstance(b, six.binary_type):
            raise TypeError("input must be a binary string")

        self.lines.append(b)
        self.chunk_bytes += len(b)
        self.total_size += len(b)

        if self.chunk_bytes >= self.min_part_size:
            buff = b"".join(self.lines)
            logger.info("uploading part #%i, %i bytes (total %.3fGB)" % (self.parts, len(buff), self.total_size / 1024.0 ** 3))
            self.upload(buff)
            logger.debug("upload of part #%i finished" % self.parts)
            self.parts += 1
            self.lines, self.chunk_bytes = [], 0

    def seek(self, offset, whence=None):
        raise NotImplementedError("seek() not implemented yet")

    def close(self):
        buff = b"".join(self.lines)
        if buff:
            logger.info("uploading last part #%i, %i bytes (total %.3fGB)" % (self.parts, len(buff), self.total_size / 1024.0 ** 3))
            self.upload(buff)
            logger.debug("upload of last part #%i finished" % self.parts)
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, type, value, traceback):
        self.close()


def s3_iter_bucket_process_key_with_kwargs(kwargs):
    return s3_iter_bucket_process_key(**kwargs)


def s3_iter_bucket_process_key(key, retries=3):
    """
    Conceptually part of `s3_iter_bucket`, but must remain top-level method because
    of pickling visibility.

    """
    # Sometimes, https://github.com/boto/boto/issues/2409 can happen because of network issues on either side.
    # Retry up to 3 times to ensure its not a transient issue.
    for x in range(0, retries + 1):
        try:
            return key, key.get_contents_as_string()
        except SSLError:
            # Actually fail on last pass through the loop
            if x == retries:
                raise
            # Otherwise, try again, as this might be a transient timeout
            pass


def s3_iter_bucket(bucket, prefix='', accept_key=lambda key: True, key_limit=None, workers=16, retries=3):
    """
    Iterate and download all S3 files under `bucket/prefix`, yielding out
    `(key, key content)` 2-tuples (generator).

    `accept_key` is a function that accepts a key name (unicode string) and
    returns True/False, signalling whether the given key should be downloaded out or
    not (default: accept all keys).

    If `key_limit` is given, stop after yielding out that many results.

    The keys are processed in parallel, using `workers` processes (default: 16),
    to speed up downloads greatly. If multiprocessing is not available, thus
    MULTIPROCESSING is False, this parameter will be ignored.

    Example::

      >>> mybucket = boto.connect_s3().get_bucket('mybucket')

      >>> # get all JSON files under "mybucket/foo/"
      >>> for key, content in s3_iter_bucket(mybucket, prefix='foo/', accept_key=lambda key: key.endswith('.json')):
      ...     print key, len(content)

      >>> # limit to 10k files, using 32 parallel workers (default is 16)
      >>> for key, content in s3_iter_bucket(mybucket, key_limit=10000, workers=32):
      ...     print key, len(content)

    """
    total_size, key_no = 0, -1
    keys = ({'key': key, 'retries': retries} for key in bucket.list(prefix=prefix) if accept_key(key.name))

    if MULTIPROCESSING:
        logger.info("iterating over keys from %s with %i workers" % (bucket, workers))
        pool = multiprocessing.pool.Pool(processes=workers)
        iterator = pool.imap_unordered(s3_iter_bucket_process_key_with_kwargs, keys)
    else:
        logger.info("iterating over keys from %s without multiprocessing" % bucket)
        iterator = imap(s3_iter_bucket_process_key_with_kwargs, keys)

    for key_no, (key, content) in enumerate(iterator):
        if key_no % 1000 == 0:
            logger.info("yielding key #%i: %s, size %i (total %.1fMB)" %
                (key_no, key, len(content), total_size / 1024.0 ** 2))

        yield key, content
        key.close()
        total_size += len(content)

        if key_limit is not None and key_no + 1 >= key_limit:
            # we were asked to output only a limited number of keys => we're done
            break

    if MULTIPROCESSING:
        pool.terminate()

    logger.info("processed %i keys, total size %i" % (key_no + 1, total_size))


class WebHdfsException(Exception):
    def __init__(self, msg=str()):
        self.msg = msg
        super(WebHdfsException, self).__init__(self.msg)
