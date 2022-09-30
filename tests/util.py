# Test utilities and common settings

from contextlib import contextmanager
from functools import partial
from http.server import SimpleHTTPRequestHandler
import importlib.machinery
import importlib.util
import logging
import os
import shlex
import subprocess
import sys
from threading import Thread

try:
    from http.server import ThreadingHTTPServer
except ImportError:
    # ThreadingHTTPServer was only added in Python 3.7.
    from http.server import HTTPServer
    from socketserver import ThreadingMixIn

    class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
        daemon_threads = True

logger = logging.getLogger(__name__)

# Common directories
TESTSDIR = os.path.dirname(__file__)
SRCDIR = os.path.dirname(TESTSDIR)
LIBDIR = os.path.join(SRCDIR, 'lib')

# Test GPG keys in tests/data
TEST_KEY_IDS = {
    'test1': '5492C5F677139E42',
    'test2': '292CDFA9EE1156B5',
    'test3': '41FC13C44703AFF0',
}


def import_script(name, script):
    """Import a script as a module"""
    spec = importlib.util.spec_from_loader(
        name,
        importlib.machinery.SourceFileLoader(name, script)
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# shlex.join added in python 3.8.
if hasattr(shlex, 'join'):
    _join_cmd = shlex.join
else:
    def _join_cmd(cmd):
        return ' '.join([shlex.quote(arg) for arg in cmd])


def run_command(cmd, check=True, **kwargs):
    """subprocess.run wrapper with logging"""
    logger.debug('$ %s', _join_cmd(cmd))
    return subprocess.run(cmd, check=check, **kwargs)


# Monkey patch SimpleHTTPRequestHandler to handle directory keyword arg
# if python is less than 3.7.
if sys.version_info[0:2] < (3, 7):
    import urllib.parse

    _SimpleHTTPRequestHandler = SimpleHTTPRequestHandler

    class SimpleHTTPRequestHandler(_SimpleHTTPRequestHandler):
        def __init__(self, *args, directory=None, **kwargs):
            if directory is None:
                directory = os.getcwd()
            self.directory = os.fspath(directory)
            super().__init__(*args, **kwargs)

        def translate_path(self, path):
            """Translate a /-separated PATH to the local filename syntax.

            Components that mean special things to the local file system
            (e.g. drive or directory names) are ignored.  (XXX They should
            probably be diagnosed.)

            """
            # abandon query parameters
            path = path.split('?', 1)[0]
            path = path.split('#', 1)[0]
            # Don't forget explicit trailing slash when normalizing. Issue17324
            trailing_slash = path.rstrip().endswith('/')
            try:
                path = urllib.parse.unquote(path, errors='surrogatepass')
            except UnicodeDecodeError:
                path = urllib.parse.unquote(path)
            path = os.path.normpath(path)
            words = path.split('/')
            words = filter(None, words)
            path = self.directory
            for word in words:
                if os.path.dirname(word) or word in (os.curdir, os.pardir):
                    # Ignore components that are not a simple file/directory name
                    continue
                path = os.path.join(path, word)
            if trailing_slash:
                path += '/'
            logger.info('Translated path: %s', path)
            return path


class LoggerHTTPRequestHandler(SimpleHTTPRequestHandler):
    """HTTP request handler logging to a Logger"""
    def log_message(self, format, *args):
        message = format % args
        logger.debug(
            f'{self.client_address[0]}:{self.client_address[1]} {message}'
        )


@contextmanager
def http_server_thread(directory):
    """HTTP server running in separate thread"""
    handler = partial(LoggerHTTPRequestHandler, directory=directory)
    with ThreadingHTTPServer(('127.0.0.1', 0), handler) as server:
        host, port = server.socket.getsockname()
        url = f'http://{host}:{port}'
        logger.info(f'Server bound to {url}')

        # Start the server loop in a separate thread.
        logger.debug('Starting HTTP server')
        thread = Thread(target=server.serve_forever)
        thread.start()
        try:
            # Yield to the caller with the bound socket name.
            yield url
        finally:
            # Shutdown the server and wait for the thread to end after
            # the server handles the shutdown request.
            logger.debug('Shutting down HTTP server')
            server.shutdown()
            thread.join(5)
