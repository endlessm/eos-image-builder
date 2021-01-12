# Test utilities and common settings

import importlib.machinery
import importlib.util
import os

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
