# Tests for eibostree module

import logging
import pytest
import shutil

from ..util import http_server_thread, run_command

logger = logging.getLogger(__name__)


HAVE_PREREQS = True
try:
    import gi
except ImportError:
    logger.debug('Missing python gi library')
    HAVE_PREREQS = False

if HAVE_PREREQS:
    try:
        gi.require_version('OSTree', '1.0')
        # Only used in eibostree
        from gi.repository import Gio, GLib, OSTree  # noqa: F401
    except ValueError as err:
        logger.debug('Missing OSTree GI bindings: %s', err)
        HAVE_PREREQS = False

if HAVE_PREREQS:
    if not shutil.which('ostree'):
        logger.debug('Missing ostree CLI program')
        HAVE_PREREQS = False

if not HAVE_PREREQS:
    pytest.skip('Missing eibostree prerequisites', allow_module_level=True)

import eibostree  # noqa: E402


@pytest.fixture
def local_ostree_repo_path(tmp_path):
    path = tmp_path / 'local-ostree-repo'
    path.mkdir()
    return path


@pytest.fixture
def local_ostree_repo(local_ostree_repo_path):
    repo_file = Gio.File.new_for_path(str(local_ostree_repo_path))
    repo = OSTree.Repo.new(repo_file)
    repo.create(OSTree.RepoMode.ARCHIVE)
    return repo


@pytest.fixture
def remote_ostree_repo_path(tmp_path):
    path = tmp_path / 'remote-ostree-repo'
    path.mkdir()
    return path


@pytest.fixture
def remote_ostree_repo(remote_ostree_repo_path):
    repo_file = Gio.File.new_for_path(str(remote_ostree_repo_path))
    repo = OSTree.Repo.new(repo_file)
    repo.set_collection_id('com.example.OSRepo')
    repo.create(OSTree.RepoMode.ARCHIVE)
    return repo


@pytest.fixture
def remote_ostree_server(remote_ostree_repo_path, remote_ostree_repo):
    with http_server_thread(remote_ostree_repo_path) as url:
        yield {
            'path': remote_ostree_repo_path,
            'repo': remote_ostree_repo,
            'url': url,
        }


def test_fetch_remote_collection_id(local_ostree_repo, remote_ostree_server):
    """Test fetch_remote_collection_id"""
    local_ostree_repo.remote_add('test', remote_ostree_server['url'])

    remote_repo = remote_ostree_server['repo']
    remote_repo.set_collection_id('com.example.Test')
    remote_repo.write_config(
        remote_repo.copy_config()
    )
    run_command([
        'ostree',
        f'--repo={remote_ostree_server["path"]}',
        'summary',
        '--update',
    ])
    collection_id = eibostree.fetch_remote_collection_id(
        local_ostree_repo, 'test'
    )
    assert collection_id == 'com.example.Test'

    remote_repo.set_collection_id(None)
    remote_repo.write_config(
        remote_repo.copy_config()
    )
    run_command([
        'ostree',
        f'--repo={remote_ostree_server["path"]}',
        'summary',
        '--update',
    ])
    collection_id = eibostree.fetch_remote_collection_id(
        local_ostree_repo, 'test'
    )
    assert collection_id is None
