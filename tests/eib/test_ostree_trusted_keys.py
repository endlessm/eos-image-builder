# Tests for eib ostree trusted key handling

import eib
import os
import pytest
import shutil

from ..util import TESTSDIR


@pytest.fixture
def keys_config(tmp_path, config):
    config['build']['tmpdir'] = str(tmp_path)

    datadir = tmp_path / 'data'
    config['build']['datadir'] = str(datadir)

    localdatadir = tmp_path / 'local' / 'data'
    config['build']['localdatadir'] = str(localdatadir)

    return config


def test_errors(keys_config):
    """Test errors from get_ostree_trusted_keys"""
    with pytest.raises(eib.ImageBuildError, match='No gpg keys directories'):
        eib.get_ostree_trusted_keys(keys_config)

    os.makedirs(os.path.join(keys_config['build']['datadir'], 'keys'))
    os.makedirs(os.path.join(keys_config['build']['localdatadir'], 'keys'))
    with pytest.raises(eib.ImageBuildError, match='No gpg keys in'):
        eib.get_ostree_trusted_keys(keys_config)


def test_get_keys(keys_config):
    """Test the keys are gathered correctly"""
    keysdir = os.path.join(keys_config['build']['datadir'], 'keys')
    localkeysdir = os.path.join(keys_config['build']['localdatadir'],
                                'keys')
    testdatadir = os.path.join(TESTSDIR, 'data')
    os.makedirs(keysdir)
    os.makedirs(localkeysdir)

    shutil.copy2(os.path.join(testdatadir, 'test1.asc'), keysdir)
    keys = eib.get_ostree_trusted_keys(keys_config)
    assert keys == [os.path.join(keysdir, 'test1.asc')]

    shutil.copy2(os.path.join(testdatadir, 'test2.asc'), keysdir)
    keys = eib.get_ostree_trusted_keys(keys_config)
    assert keys == [
        os.path.join(keysdir, 'test1.asc'),
        os.path.join(keysdir, 'test2.asc'),
    ]

    shutil.copy2(os.path.join(testdatadir, 'test3.asc'), localkeysdir)
    keys = eib.get_ostree_trusted_keys(keys_config)
    assert keys == [
        os.path.join(keysdir, 'test1.asc'),
        os.path.join(keysdir, 'test2.asc'),
        os.path.join(localkeysdir, 'test3.asc'),
    ]
