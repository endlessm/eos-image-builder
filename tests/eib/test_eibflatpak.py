# Tests for eibflatpak module

from base64 import b64encode
import logging
import os
import pytest
import shutil
import subprocess
from textwrap import dedent
from urllib.parse import urljoin

from ..util import (
    TESTSDIR,
    TEST_KEY_IDS,
    http_server_thread,
    run_command,
)

logger = logging.getLogger(__name__)


FLATPAKS = [
    ('platform-1', '1'),
    ('platform-1-locale', '1'),
    ('platform-2', '2'),
    ('platform-2-locale', '2'),
    ('app-1', 'master'),
    ('app-1-locale', 'master'),
    ('app-2', 'master'),
    ('app-2-locale', 'master'),
    ('app-extra-data', 'master'),
]


HAVE_PREREQS = True
try:
    import gi
except ImportError:
    logger.debug('Missing python gi library')
    HAVE_PREREQS = False

if HAVE_PREREQS:
    try:
        gi.require_version('Flatpak', '1.0')
        gi.require_version('OSTree', '1.0')
        # Only used in eibflatpak
        from gi.repository import Flatpak, GLib  # noqa: F401
        from gi.repository import Gio, OSTree
    except (ImportError, ValueError) as err:
        logger.debug('Missing GI bindings: %s', err)
        HAVE_PREREQS = False

if HAVE_PREREQS:
    if not shutil.which('flatpak'):
        logger.debug('Missing flatpak CLI program')
        HAVE_PREREQS = False

if not HAVE_PREREQS:
    pytest.skip('Missing eibflatpak prerequisites', allow_module_level=True)

import eibflatpak  # noqa: E402


@pytest.fixture
def local_flatpak_installation_path(tmp_path):
    path = tmp_path / 'local-flatpak-inst'
    path.mkdir()
    return path


@pytest.fixture
def local_flatpak_installation(local_flatpak_installation_path):
    inst_file = Gio.File.new_for_path(str(local_flatpak_installation_path))
    inst = Flatpak.Installation.new_for_path(inst_file, user=True)
    return inst


@pytest.fixture
def remote_flatpak_repo_path(tmp_path):
    path = tmp_path / 'remote-flatpak-repo'
    path.mkdir()
    return path


@pytest.fixture
def remote_flatpak_repo(remote_flatpak_repo_path):
    repo_file = Gio.File.new_for_path(str(remote_flatpak_repo_path))
    repo = OSTree.Repo.new(repo_file)
    repo.set_collection_id('com.example.FlatpakRepo')
    repo.create(OSTree.RepoMode.ARCHIVE)
    return repo


@pytest.fixture
def remote_flatpak_server(remote_flatpak_repo_path, remote_flatpak_repo,
                          builder_gpgdir):
    export_cmd = (
        'gpg', '--homedir', str(builder_gpgdir),
        '--export', TEST_KEY_IDS['test1']
    )
    proc = run_command(export_cmd, check=True, stdout=subprocess.PIPE)
    gpg_key_b64 = b64encode(proc.stdout).decode('ascii')

    with http_server_thread(remote_flatpak_repo_path) as url:
        flatpakrepo_path = remote_flatpak_repo_path / 'example.flatpakrepo'
        flatpakrepo_url = urljoin(url, flatpakrepo_path.name)
        flatpakrepo_content = dedent(
            f"""\
            [Flatpak Repo]
            Version=1
            Url={url}
            Title=Example Repo
            DefaultBranch=master
            GPGKey={gpg_key_b64}
            """
        )
        with open(flatpakrepo_path, 'w') as f:
            f.write(flatpakrepo_content)

        yield {
            'path': remote_flatpak_repo_path,
            'repo': remote_flatpak_repo,
            'url': url,
            'flatpakrepo_url': flatpakrepo_url,
        }


def build_flatpak(srcdir, builddir, repodir, branch):
    """Build a flatpak from static data"""
    logger.info(f'Building {srcdir} branch {branch}')
    shutil.rmtree(builddir, ignore_errors=True)
    shutil.copytree(srcdir, builddir)
    builddir.joinpath('files').mkdir(exist_ok=True)
    builddir.joinpath('usr').mkdir(exist_ok=True)
    run_command(('flatpak', 'build-finish', str(builddir)))
    run_command(
        ('flatpak', 'build-export', str(repodir), str(builddir), branch),
    )


def get_installation_repo(installation):
    """Get the OSTree repo for a Flatpak installation"""
    repo_file = installation.get_path().get_child('repo')
    repo = OSTree.Repo.new(repo_file)
    repo.open()
    return repo


def _get_commit_dir_files(directory):
    enumerator = directory.enumerate_children(
        'standard::name,standard::type,standard::size,standard::is-symlink,'
        'standard::symlink-target',
        Gio.FileQueryInfoFlags.NOFOLLOW_SYMLINKS,
    )
    while True:
        child_info = enumerator.next_file()
        if child_info is None:
            break

        child = enumerator.get_child(child_info)
        child_path = child.get_path()
        try:
            child.ensure_resolved()
        except GLib.GError as err:
            logger.debug(
                '%s commit %s missing: %s',
                child.get_checksum(),
                child_path,
                err
            )
            continue

        child_type = child_info.get_file_type()
        child_target = child_info.get_symlink_target()
        yield (child_path, child_type, child_target)
        if child_type == Gio.FileType.DIRECTORY:
            yield from _get_commit_dir_files(child)


def get_commit_files(repo, ref):
    logger.debug('Getting files for %s', ref)
    _, root, _ = repo.read_commit(ref)
    return _get_commit_dir_files(root)


@pytest.fixture(scope='session')
def flatpak_build_repo(tmp_path_factory):
    """OSTree repo populated with built flatpaks"""
    repo_path = tmp_path_factory.getbasetemp() / 'flatpak-build-repo'
    repo_path.mkdir()
    repo = OSTree.Repo.new(Gio.File.new_for_path(str(repo_path)))
    repo.create(OSTree.RepoMode.ARCHIVE)

    build_path = tmp_path_factory.getbasetemp() / 'flatpak-build'
    for src, branch in FLATPAKS:
        srcdir = os.path.join(TESTSDIR, 'data/flatpak', src)
        build_flatpak(srcdir, build_path, repo_path, branch)

    return repo


@pytest.fixture
def full_remote_flatpak_server(flatpak_build_repo, remote_flatpak_server,
                               builder_gpgdir):
    """Flatpak remote server populated with built flatpaks"""
    _, all_refs = flatpak_build_repo.list_refs(None)
    logger.info(f'All build refs: {all_refs}')

    _, app_refs = flatpak_build_repo.list_refs_ext(
        'app',
        OSTree.RepoListRefsExtFlags.NONE,
    )
    _, runtime_refs = flatpak_build_repo.list_refs_ext(
        'runtime',
        OSTree.RepoListRefsExtFlags.NONE,
    )
    build_refs = sorted(app_refs.keys() | runtime_refs.keys())
    logger.info(f'Build refs: {build_refs}')

    src_repo = flatpak_build_repo.get_path().get_path()
    for ref in build_refs:
        run_command((
            'flatpak',
            'build-commit-from',
            f'--src-repo={src_repo}',
            f'--gpg-sign={TEST_KEY_IDS["test1"]}',
            f'--gpg-homedir={builder_gpgdir}',
            '--no-update-summary',
            f'{remote_flatpak_server["path"]}',
            ref,
        ))

    run_command((
        'flatpak',
        'build-update-repo',
        f'--gpg-sign={TEST_KEY_IDS["test1"]}',
        f'--gpg-homedir={builder_gpgdir}',
        str(remote_flatpak_server['path']),
    ))

    _, all_remote_refs = remote_flatpak_server['repo'].list_refs(None)
    logger.debug(f'All remote refs: {all_remote_refs}')

    return remote_flatpak_server


@pytest.fixture
def flatpak_config(builder_config, remote_flatpak_server):
    """Image builder config with test flatpak configuration"""
    builder_config.add_section('flatpak')
    builder_config['flatpak'].update({
        'enable': 'true',
        'arch': 'x86_64',
        'locales': 'en es',
    })
    builder_config.add_section('flatpak-remote-example')
    builder_config['flatpak-remote-example'].update({
        'repo_file': remote_flatpak_server['flatpakrepo_url'],
        'apps': ' '.join([
            'com.example.App1',
            'com.example.App2',
        ]),
    })
    return builder_config


def test_pull(local_flatpak_installation, flatpak_config,
              full_remote_flatpak_server):
    """Pull to cache repo

    This is approximately what hooks/content/50-flatpak does.
    """
    manager = eibflatpak.FlatpakManager(
        local_flatpak_installation,
        config=flatpak_config,
        is_cache_repo=True,
    )
    manager.add_remotes()
    manager.enumerate_remotes()
    manager.pull()

    installed_refs = {
        ref.format_ref() for ref in
        local_flatpak_installation.list_installed_refs()
    }
    assert installed_refs == set()

    inst_repo = get_installation_repo(local_flatpak_installation)
    _, inst_repo_refs = inst_repo.list_refs(None)
    expected_repo_refs = {
        'example:app/com.example.App1/x86_64/master',
        'example:runtime/com.example.App1.Locale/x86_64/master',
        'example:app/com.example.App2/x86_64/master',
        'example:runtime/com.example.App2.Locale/x86_64/master',
        'example:runtime/com.example.Platform/x86_64/1',
        'example:runtime/com.example.Platform.Locale/x86_64/1',
        'example:runtime/com.example.Platform/x86_64/2',
        'example:runtime/com.example.Platform.Locale/x86_64/2',
    }
    assert inst_repo_refs.keys() == expected_repo_refs

    # Ensure the expected locale files have been pulled.
    locale_refs = [
        ref for ref in expected_repo_refs if '.Locale' in ref
    ]
    for ref in locale_refs:
        files = set(get_commit_files(inst_repo, ref))
        logger.debug('Commit %s files:\n%s', ref, files)
        assert ('/files/en', Gio.FileType.DIRECTORY, None) in files
        assert ('/files/es', Gio.FileType.DIRECTORY, None) in files
        assert ('/files/fr', Gio.FileType.DIRECTORY, None) not in files


def test_install(local_flatpak_installation, flatpak_config,
                 full_remote_flatpak_server):
    """Install flatpaks

    This is approximately what hooks/image/50-flatpak.chroot does.
    """
    manager = eibflatpak.FlatpakManager(
        local_flatpak_installation,
        config=flatpak_config,
    )
    manager.add_remotes()
    manager.enumerate_remotes()
    manager.install()

    installed_refs = {
        ref.format_ref() for ref in
        local_flatpak_installation.list_installed_refs()
    }
    assert installed_refs == {
        'app/com.example.App1/x86_64/master',
        'runtime/com.example.App1.Locale/x86_64/master',
        'app/com.example.App2/x86_64/master',
        'runtime/com.example.App2.Locale/x86_64/master',
        'runtime/com.example.Platform/x86_64/1',
        'runtime/com.example.Platform.Locale/x86_64/1',
        'runtime/com.example.Platform/x86_64/2',
        'runtime/com.example.Platform.Locale/x86_64/2',
    }

    inst_repo = get_installation_repo(local_flatpak_installation)
    _, inst_repo_refs = inst_repo.list_refs(None)
    assert inst_repo_refs.keys() == {
        'deploy/app/com.example.App1/x86_64/master',
        'deploy/runtime/com.example.App1.Locale/x86_64/master',
        'deploy/app/com.example.App2/x86_64/master',
        'deploy/runtime/com.example.App2.Locale/x86_64/master',
        'deploy/runtime/com.example.Platform/x86_64/1',
        'deploy/runtime/com.example.Platform.Locale/x86_64/1',
        'deploy/runtime/com.example.Platform/x86_64/2',
        'deploy/runtime/com.example.Platform.Locale/x86_64/2',
        'example:app/com.example.App1/x86_64/master',
        'example:runtime/com.example.App1.Locale/x86_64/master',
        'example:app/com.example.App2/x86_64/master',
        'example:runtime/com.example.App2.Locale/x86_64/master',
        'example:runtime/com.example.Platform/x86_64/1',
        'example:runtime/com.example.Platform.Locale/x86_64/1',
        'example:runtime/com.example.Platform/x86_64/2',
        'example:runtime/com.example.Platform.Locale/x86_64/2',
    }

    # Ensure the expected locale subpaths have been installed.
    locale_refs = [
        ref for ref in
        local_flatpak_installation.list_installed_refs()
        if ref.get_name().endswith('.Locale')
    ]
    for ref in locale_refs:
        subpaths = ref.get_subpaths()
        assert subpaths == ['/en', '/es']


def test_resolve(local_flatpak_installation, flatpak_config,
                 full_remote_flatpak_server):
    manager = eibflatpak.FlatpakManager(
        local_flatpak_installation,
        config=flatpak_config,
    )
    manager.add_remotes()
    manager.enumerate_remotes()
    resolved_full_refs = manager.resolve()

    resolved_refs = {fr.ref for fr in resolved_full_refs}
    assert resolved_refs == {
        'app/com.example.App1/x86_64/master',
        'runtime/com.example.App1.Locale/x86_64/master',
        'app/com.example.App2/x86_64/master',
        'runtime/com.example.App2.Locale/x86_64/master',
        'runtime/com.example.Platform/x86_64/1',
        'runtime/com.example.Platform.Locale/x86_64/1',
        'runtime/com.example.Platform/x86_64/2',
        'runtime/com.example.Platform.Locale/x86_64/2',
    }


def test_seed(local_flatpak_installation, flatpak_config, full_remote_flatpak_server,
              tmp_path):
    """Seed from cache repo"""
    cache_repo_path = tmp_path / 'cache-repo'
    cache_repo = OSTree.Repo.new(Gio.File.new_for_path(str(cache_repo_path)))
    cache_repo.create(OSTree.RepoMode.ARCHIVE)

    remote_repo_uri = full_remote_flatpak_server['path'].as_uri()
    app_ref = 'app/com.example.App1/x86_64/master'
    locale_ref = 'runtime/com.example.App1.Locale/x86_64/master'
    run_command((
        'ostree',
        f'--repo={cache_repo_path}',
        'remote',
        'add',
        '--no-gpg-verify',
        'origin',
        remote_repo_uri,
    ))
    run_command((
        'ostree',
        f'--repo={cache_repo_path}',
        'pull',
        'origin',
        app_ref,
    ))
    run_command((
        'ostree',
        f'--repo={cache_repo_path}',
        'pull',
        '--subpath=/metadata',
        '--subpath=/files/en',
        'origin',
        locale_ref,
    ))

    _, cache_repo_refs = cache_repo.list_refs(None)
    logger.debug('Cache repo refs: %s', cache_repo_refs)

    manager = eibflatpak.FlatpakManager(
        local_flatpak_installation,
        config=flatpak_config,
    )
    manager.add_remotes()
    manager.enumerate_remotes()
    manager.seed(str(cache_repo_path), cache_repo_refs)

    inst_repo = get_installation_repo(local_flatpak_installation)
    _, inst_repo_refs = inst_repo.list_refs(None)
    inst_repo_commits = {
        OSTree.object_name_deserialize(commit_variant)[0]
        for commit_variant in
        inst_repo.list_commit_objects_starting_with('')[1].keys()
    }

    # There should be no refs and only the full app commit should be
    # seeded.
    assert inst_repo_refs == {}
    app_rev = cache_repo_refs[f'origin:{app_ref}']
    assert inst_repo_commits == {app_rev}


def test_install_seed(local_flatpak_installation, flatpak_config,
                      full_remote_flatpak_server, tmp_path):
    """Install with seeding from cache repo"""
    cache_repo_path = tmp_path / 'cache-repo'
    cache_repo_uri = cache_repo_path.as_uri()
    cache_repo = OSTree.Repo.new(Gio.File.new_for_path(str(cache_repo_path)))
    cache_repo.create(OSTree.RepoMode.ARCHIVE)

    remote_repo = full_remote_flatpak_server['repo']
    remote_repo_path = full_remote_flatpak_server['path']
    remote_repo_uri = remote_repo_path.as_uri()
    app_ref = 'app/com.example.App1/x86_64/master'
    locale_ref = 'runtime/com.example.App2.Locale/x86_64/master'
    run_command((
        'ostree',
        f'--repo={cache_repo_path}',
        'remote',
        'add',
        '--no-gpg-verify',
        'origin',
        remote_repo_uri,
    ))
    run_command((
        'ostree',
        f'--repo={cache_repo_path}',
        'pull',
        'origin',
        app_ref,
    ))
    run_command((
        'ostree',
        f'--repo={cache_repo_path}',
        'pull',
        '--subpath=/metadata',
        '--subpath=/files/en',
        'origin',
        locale_ref,
    ))

    _, cache_repo_refs = cache_repo.list_refs(None)
    logger.debug('Cache repo refs: %s', cache_repo_refs)

    # Now prune the commits from the remote repo and pull back only the
    # commit so that the cache repo is required.
    remote_repo.set_ref_immediate(None, app_ref, None)
    remote_repo.set_ref_immediate(None, locale_ref, None)
    remote_repo.prune(OSTree.RepoPruneFlags.REFS_ONLY, 0)
    app_rev = cache_repo_refs[f'origin:{app_ref}']
    locale_rev = cache_repo_refs[f'origin:{locale_ref}']
    run_command((
        'ostree',
        f'--repo={remote_repo_path}',
        'remote',
        'add',
        '--no-gpg-verify',
        'cache',
        cache_repo_uri,
    ))
    run_command((
        'ostree',
        f'--repo={remote_repo_path}',
        'pull',
        '--commit-metadata-only',
        'cache',
        app_rev,
    ))
    run_command((
        'ostree',
        f'--repo={remote_repo_path}',
        'pull',
        '--commit-metadata-only',
        'cache',
        locale_rev,
    ))
    remote_repo.set_ref_immediate(None, app_ref, app_rev)
    remote_repo.set_ref_immediate(None, locale_ref, locale_rev)

    # Installing App1 should succeed as the cache repo has all the
    # objects.
    flatpak_config['flatpak-remote-example']['apps'] = 'com.example.App1'
    manager = eibflatpak.FlatpakManager(
        local_flatpak_installation,
        config=flatpak_config,
    )
    manager.add_remotes()
    manager.enumerate_remotes()
    manager.install(str(cache_repo_path))

    # Installing App2 should fail as the partial locale commit will not
    # be seeded from the cache repo.
    flatpak_config['flatpak-remote-example']['apps'] = 'com.example.App2'
    manager = eibflatpak.FlatpakManager(
        local_flatpak_installation,
        config=flatpak_config,
    )
    manager.add_remotes()
    manager.enumerate_remotes()
    with pytest.raises(GLib.GError) as excinfo:
        manager.install(str(cache_repo_path))
    assert excinfo.value.matches(Flatpak.Error.quark(), Flatpak.Error.ABORTED)


def test_deploy_remote(local_flatpak_installation, flatpak_config,
                       full_remote_flatpak_server):
    """Deploy remotes to final state"""
    pull_url = full_remote_flatpak_server['url']
    deploy_url = pull_url + '/'

    flatpak_config['flatpak'].update({
        'enable_p2p_updates': 'true',
    })
    flatpak_config['flatpak-remote-example'].update({
        'deploy_url': deploy_url,
        'title': 'Some Title',
        'default_branch': 'somebranch',
    })
    manager = eibflatpak.FlatpakManager(
        local_flatpak_installation,
        config=flatpak_config,
    )

    manager.add_remotes()
    all_remotes = local_flatpak_installation.list_remotes()
    assert len(all_remotes) == 1
    remote = all_remotes[0]
    assert remote.get_url() == pull_url
    assert remote.get_collection_id() is None
    assert remote.get_title() == 'Some Title'
    assert remote.get_default_branch() == 'somebranch'

    manager.deploy_remotes()
    all_remotes = local_flatpak_installation.list_remotes()
    assert len(all_remotes) == 1
    remote = all_remotes[0]
    assert remote.get_url() == deploy_url
    assert remote.get_collection_id() == 'com.example.FlatpakRepo'
    assert remote.get_title() == 'Some Title'
    assert remote.get_default_branch() == 'somebranch'


def test_exclude(local_flatpak_installation, flatpak_config,
                 full_remote_flatpak_server):
    """Excluded flatpak should raise error"""
    flatpak_config['flatpak-remote-example'].update({
        'apps': 'com.example.App1',
        'exclude': 'com.example.Platform',
    })
    manager = eibflatpak.FlatpakManager(
        local_flatpak_installation,
        config=flatpak_config,
    )
    manager.add_remotes()
    manager.enumerate_remotes()
    with pytest.raises(
        eibflatpak.FlatpakError,
        match='^Excluded refs.*runtime/com.example.Platform/x86_64/1',
    ):
        manager.resolve()

    # An excluded related ref (extension) should not cause an error.
    flatpak_config['flatpak-remote-example']['exclude'] = (
        'com.example.App1.Locale'
    )
    manager = eibflatpak.FlatpakManager(
        local_flatpak_installation,
        config=flatpak_config,
    )
    manager.add_remotes()
    manager.enumerate_remotes()
    resolved_full_refs = manager.resolve()
    resolved_refs = [fr.ref for fr in resolved_full_refs]
    assert 'runtime/com.example.App1.Locale/x86_64/master' not in resolved_refs


def test_extra_data(local_flatpak_installation, flatpak_config,
                    full_remote_flatpak_server):
    """Extra data flatpak handling"""
    flatpak_config['flatpak-remote-example'].update({
        'apps': 'com.example.AppExtraData',
    })
    manager = eibflatpak.FlatpakManager(
        local_flatpak_installation,
        config=flatpak_config,
    )
    manager.add_remotes()
    manager.enumerate_remotes()
    with pytest.raises(
        eibflatpak.FlatpakError,
        match='^Extra data.*app/com.example.AppExtraData/x86_64/master',
    ):
        manager.resolve()

    flatpak_config['flatpak-remote-example'].update({
        'allow_extra_data': 'com.example.AppExtraData',
    })
    manager = eibflatpak.FlatpakManager(
        local_flatpak_installation,
        config=flatpak_config,
    )
    manager.add_remotes()
    manager.enumerate_remotes()
    resolved_full_refs = manager.resolve()
    resolved_refs = [fr.ref for fr in resolved_full_refs]
    assert 'app/com.example.AppExtraData/x86_64/master' in resolved_refs


def test_eol(local_flatpak_installation, flatpak_config, full_remote_flatpak_server,
             builder_gpgdir, caplog):
    """EOL flatpak handling"""
    repo_path = str(full_remote_flatpak_server['path'])
    ref = 'app/com.example.App1/x86_64/master'

    # Set the old runtime EOL
    run_command((
        'flatpak',
        'build-commit-from',
        f'--src-repo={repo_path}',
        f'--gpg-sign={TEST_KEY_IDS["test1"]}',
        f'--gpg-homedir={builder_gpgdir}',
        '--end-of-life=Dead',
        repo_path,
        ref,
    ))

    manager = eibflatpak.FlatpakManager(
        local_flatpak_installation,
        config=flatpak_config,
    )
    manager.add_remotes()
    manager.enumerate_remotes()
    resolved_full_refs = manager.resolve()
    resolved_refs = [fr.ref for fr in resolved_full_refs]
    expected_record = (
        eibflatpak.logger.name,
        logging.WARNING,
        'app/com.example.App1/x86_64/master in example is marked as EOL: Dead'
    )
    assert expected_record in caplog.record_tuples
    assert ref in resolved_refs

    # Add EOL rebase
    run_command((
        'flatpak',
        'build-commit-from',
        f'--src-repo={repo_path}',
        f'--gpg-sign={TEST_KEY_IDS["test1"]}',
        f'--gpg-homedir={builder_gpgdir}',
        '--end-of-life=Dead',
        '--end-of-life-rebase=com.example.App1=com.example.App2',
        repo_path,
        ref,
    ))

    manager = eibflatpak.FlatpakManager(
        local_flatpak_installation,
        config=flatpak_config,
    )
    manager.add_remotes()
    manager.enumerate_remotes()
    with pytest.raises(
        eibflatpak.FlatpakError,
        match='^Refs marked eol-rebase.*',
    ):
        manager.resolve()
