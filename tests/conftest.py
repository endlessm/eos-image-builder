# pytest fixtures
# https://docs.pytest.org/en/stable/fixture.html

import datetime
import os
import pytest
import shutil
import sys

from .util import LIBDIR, SRCDIR, import_script

run_build = import_script('run_build', os.path.join(SRCDIR, 'run-build'))

# Add the lib directory to the path so eib can be imported here and in
# other test modules.
sys.path.insert(1, LIBDIR)
import eib  # noqa: E402


@pytest.fixture
def config():
    """Provide an ImageConfigParser instance"""
    return eib.ImageConfigParser()


@pytest.fixture
def builder_config(config):
    """Provide an ImageBuilder config instance

    This fills in the build section like ImageBuilder so that
    interpolation of full sections should succeed.
    """
    config[config.defaultsect].update({
        'product': 'eos',
        'branch': 'master',
        'arch': 'amd64',
        'platform': 'amd64',
        'personality': 'base',
        'force': 'false',
        'dry_run': 'false',
        'series': 'master',
        'srcdir': SRCDIR,
        'datadir': '${srcdir}/data',
        'helpersdir': '${srcdir}/helpers',
        'cachedir': eib.CACHEDIR,
        'tmpdir': '${cachedir}/tmp',
        'contentdir': '${cachedir}/content',
        'outrootdir': '${tmpdir}/out',
        'outdir': '${outrootdir}/${personality}',
        'configdir': '${srcdir}/config',
        'sysconfdir': eib.SYSCONFDIR,
        'build_version': '200101-000000',
        'outversion': ('${product}-${branch}-${arch}-${platform}'
                       '.${build_version}.${personality}'),
        'tmpconfig': '${tmpdir}/config.ini',
        'tmpfullconfig': '${tmpdir}/fullconfig.ini',
        'baselib': '${srcdir}/lib/eib.sh',
        'deb_host_gnu_cpu': 'x86_64',
        'deb_host_multiarch': 'x86_64-linux-gnu',
        'ssh_options': '',
        'keysdir': '${datadir}/keys',
        'keyring': '${tmpdir}/eib-keyring.gpg',
        'manifestdir': '${tmpdir}/manifest',
        'use_production_apps': 'false',
        'use_production_ostree': 'false'
    })

    # Make sure only the intended settings from ImageBuilder are set
    test_attrs = config.defaults().keys()
    expected_attrs = set(run_build.ImageBuilder.CONFIG_ATTRS)
    assert test_attrs == expected_attrs

    return config


@pytest.fixture(scope='session')
def tmp_builder_config(tmp_path_factory):
    """Copy the config directory avoiding unwanted local files"""
    configdir = tmp_path_factory.getbasetemp() / 'config'
    shutil.copytree(os.path.join(SRCDIR, 'config'), configdir)
    for child in ('local.ini', 'private.ini'):
        path = configdir / child
        if path.exists():
            path.unlink()
    return configdir


@pytest.fixture
def tmp_builder_paths(tmp_path, monkeypatch):
    """Override image builder system paths"""
    paths = {}

    for syspath in ('cachedir', 'builddir', 'sysconfdir'):
        path = tmp_path / syspath
        path.mkdir()
        paths[syspath.upper()] = path
        monkeypatch.setattr(eib, syspath.upper(), str(path))

    return paths


@pytest.fixture
def mock_datetime(monkeypatch):
    """Mock datetime class with fixed utcnow"""
    class MockDatetime(datetime.datetime):
        @classmethod
        def utcnow(cls):
            return cls(2000, 1, 1)

    monkeypatch.setattr(datetime, 'datetime', MockDatetime)


@pytest.fixture
def make_builder(tmp_builder_config, tmp_builder_paths, mock_datetime):
    """Factory to create ImageBuilder with defaults for required arguments"""
    def _make_builder(**kwargs):
        kwargs.setdefault('product', 'eos')
        kwargs.setdefault('branch', 'master')
        kwargs.setdefault('arch', 'amd64')
        kwargs.setdefault('platform', 'amd64')
        kwargs.setdefault('personality', 'base')
        builder = run_build.ImageBuilder(**kwargs)
        builder.configdir = str(tmp_builder_config)
        return builder
    return _make_builder
