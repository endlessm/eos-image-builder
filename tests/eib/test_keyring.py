# Tests for eib keyring handling

import eib
import os
import pytest
import shutil
import subprocess
import tempfile

from ..util import TESTSDIR, TEST_KEY_IDS


@pytest.fixture
def keyring_config(tmp_path, config):
    config['build']['tmpdir'] = str(tmp_path)

    keyring = tmp_path / 'keyring.gpg'
    config['build']['keyring'] = str(keyring)

    datadir = tmp_path / 'data'
    config['build']['datadir'] = str(datadir)

    localdatadir = tmp_path / 'local' / 'data'
    config['build']['localdatadir'] = str(localdatadir)

    return config


def test_errors(keyring_config):
    """Test errors from get_keyring"""
    with pytest.raises(eib.ImageBuildError, match='No gpg keys directories'):
        eib.get_keyring(keyring_config)

    os.makedirs(os.path.join(keyring_config['build']['datadir'], 'keys'))
    os.makedirs(os.path.join(keyring_config['build']['localdatadir'], 'keys'))
    with pytest.raises(eib.ImageBuildError, match='No gpg keys in'):
        eib.get_keyring(keyring_config)


def test_create_once(keyring_config, caplog):
    """Test keyring is only created once"""
    keysdir = os.path.join(keyring_config['build']['datadir'], 'keys')
    testdatadir = os.path.join(TESTSDIR, 'data')
    os.makedirs(keysdir)
    shutil.copy2(os.path.join(testdatadir, 'test1.asc'), keysdir)

    keyring = eib.get_keyring(keyring_config)
    assert keyring == keyring_config['build']['keyring']
    assert os.path.exists(keyring)
    assert 'Creating temporary GPG keyring' in caplog.text

    caplog.clear()
    eib.get_keyring(keyring_config)
    assert 'Creating temporary GPG keyring' not in caplog.text


def get_keyring_ids(keyring):
    """Get the public key IDs from a GPG keyring"""
    # gpg insists on creating a homedir, so appease it
    with tempfile.TemporaryDirectory() as homedir:
        output = subprocess.check_output(
            ('gpg', '--homedir', homedir, '--show-keys', '--with-colons',
             keyring)
        )
    for line in output.decode('utf-8').splitlines():
        parts = line.split(':')
        if parts[0] == 'pub':
            yield parts[4]


def test_imported_keys(keyring_config):
    """Test the keys are imported correctly"""
    keysdir = os.path.join(keyring_config['build']['datadir'], 'keys')
    localkeysdir = os.path.join(keyring_config['build']['localdatadir'],
                                'keys')
    testdatadir = os.path.join(TESTSDIR, 'data')
    os.makedirs(keysdir)
    os.makedirs(localkeysdir)

    shutil.copy2(os.path.join(testdatadir, 'test1.asc'), keysdir)
    keyring = eib.get_keyring(keyring_config)
    key_ids = set(get_keyring_ids(keyring))
    assert key_ids == {TEST_KEY_IDS['test1']}

    os.unlink(keyring)
    shutil.copy2(os.path.join(testdatadir, 'test2.asc'), keysdir)
    keyring = eib.get_keyring(keyring_config)
    key_ids = set(get_keyring_ids(keyring))
    assert key_ids == {TEST_KEY_IDS['test1'], TEST_KEY_IDS['test2']}

    os.unlink(keyring)
    shutil.copy2(os.path.join(testdatadir, 'test3.asc'), localkeysdir)
    keyring = eib.get_keyring(keyring_config)
    key_ids = set(get_keyring_ids(keyring))
    assert key_ids == set(TEST_KEY_IDS.values())


def test_verification(keyring_config, tmp_path):
    """Test that signatures can be verified from the generated keyring"""
    testdatadir = os.path.join(TESTSDIR, 'data')

    homedir = tmp_path / 'gnupg'
    homedir.mkdir(mode=0o700)
    subprocess.check_call(
        ('gpg', '--homedir', str(homedir), '--import',
         os.path.join(testdatadir, 'test1.key'))
    )

    signed_file = tmp_path / 'signed'
    subprocess.run(
        ('gpg', '--homedir', str(homedir), '--clearsign',
         '--output', str(signed_file)),
        check=True, input=b'foobar\n'
    )

    keysdir = os.path.join(keyring_config['build']['datadir'], 'keys')
    os.makedirs(keysdir)
    shutil.copy2(os.path.join(testdatadir, 'test1.asc'), keysdir)
    keyring = eib.get_keyring(keyring_config)

    subprocess.check_call(
        ('gpgv', '--keyring', keyring, str(signed_file))
    )
