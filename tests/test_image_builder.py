# Tests for ImageBuilder class

import eib
import logging
import os
import pytest

from .util import SRCDIR, import_script

run_build = import_script('run_build', os.path.join(SRCDIR, 'run-build'))

logger = logging.getLogger(__name__)


def test_config_attrs(make_builder):
    builder = make_builder()
    cases = [
        ('product', 'eos', 'eos'),
        ('branch', 'master', 'master'),
        ('arch', 'amd64', 'amd64'),
        ('platform', 'amd64', 'amd64'),
        ('personality', 'base', 'base'),
        ('srcdir', SRCDIR, SRCDIR),
        ('cachedir', eib.CACHEDIR, eib.CACHEDIR),
        ('sysconfdir', eib.SYSCONFDIR, eib.SYSCONFDIR),
        ('build_version', '000101-000000', '000101-000000'),
    ]
    for attr, expected_raw, expected_value in cases:
        assert builder.config.get('build', attr, raw=True) == expected_raw
        assert builder.config['build'][attr] == expected_value
        assert getattr(builder, attr) == expected_value


def test_setenv(make_builder, monkeypatch):
    builder = make_builder()

    # ImageBuilder.setenv() directly manipulates os.environ, so override
    # it to populate our own dictionary. However, use a context as
    # monkeypatching os.environ can break pytest.
    builder_env = {}
    with monkeypatch.context() as m:
        m.setattr(os, 'environ', builder_env)

        cases = [
            ('build', 'opt', 'val', {'EIB_OPT': 'val'}),
            ('sect', 'opt', 'val', {'EIB_SECT_OPT': 'val'}),
            ('sect', 'opt', 'True', {'EIB_SECT_OPT': 'true'}),
            ('sect', 'opt', 'False', {'EIB_SECT_OPT': 'false'}),
            ('sect', 'opt', 'a\nb', {'EIB_SECT_OPT': 'a\nb'}),
            ('sect', 'opt-a', 'val', {'EIB_SECT_OPT_A': 'val'}),
            ('sect-1', 'opt-a', 'val', {'EIB_SECT_1_OPT_A': 'val'}),
        ]

        for section, option, value, expected_environ in cases:
            builder_env.clear()
            builder.setenv(section, option, value)
            assert os.environ == expected_environ


def test_set_environment(make_builder, monkeypatch):
    builder = make_builder()

    # ImageBuilder.setenv() directly manipulates os.environ, so override
    # it to populate our own dictionary. However, use a context as
    # monkeypatching os.environ can break pytest.
    builder_env = {}
    with monkeypatch.context() as m:
        m.setattr(os, 'environ', builder_env)

        builder.config.add_section('sect')
        builder.config['sect']['opt'] = 'a\n\tb'

        cases = [
            ('EIB_PRODUCT', 'eos'),
            ('EIB_BRANCH', 'master'),
            ('EIB_ARCH', 'amd64'),
            ('EIB_PLATFORM', 'amd64'),
            ('EIB_PERSONALITY', 'base'),
            ('EIB_SRCDIR', SRCDIR),
            ('EIB_CACHEDIR', eib.CACHEDIR),
            ('EIB_SYSCONFDIR', eib.SYSCONFDIR),
            ('EIB_BUILD_VERSION', '000101-000000'),
            ('EIB_SECT_OPT', 'a\n\tb'),
        ]

        builder.set_environment()
        for envvar, value in cases:
            assert envvar in os.environ
            assert os.environ[envvar] == value


@pytest.mark.parametrize('branch,expected', [
    ('master', 'master'), ('eos3.8', 'eos3'), ('eos3', 'eos3'),
    ('eos2.4', 'eos2'),
])
def test_series(make_builder, branch, expected):
    builder = make_builder(branch=branch)
    assert builder.series == expected


@pytest.mark.parametrize('arch,platform,expected', [
    ('amd64', 'nexthw', 'nexthw'), ('amd64', None, 'amd64'),
    ('arm64', 'rpi4', 'rpi4'), ('arm64', None, 'arm64'),
    ('i386', 'i386', 'i386'), ('i386', None, 'i386'),
    ('armhf', 'ec100', 'ec100'), ('armhf', None, 'odroidu2'),
])
def test_platform(make_builder, arch, platform, expected):
    builder = make_builder(arch=arch, platform=platform)
    assert builder.platform == expected


def test_bad_arch(make_builder):
    with pytest.raises(eib.ImageBuildError,
                       match='Architecture.*not supported'):
        make_builder(arch='notanarch')


@pytest.mark.parametrize('arch,expected_gnu_cpu,expected_multiarch', [
    ('amd64', 'x86_64', 'x86_64-linux-gnu'),
    ('arm64', 'aarch64', 'aarch64-linux-gnu'),
    ('i386', 'i686', 'i386-linux-gnu'),
    ('armhf', 'arm', 'arm-linux-gnueabihf'),
])
def test_arch_details(make_builder, arch, expected_gnu_cpu,
                      expected_multiarch):
    builder = make_builder(arch=arch)
    assert builder.deb_host_gnu_cpu == expected_gnu_cpu
    assert builder.deb_host_multiarch == expected_multiarch


# Build test variants. This is ideally the same as the release image
# variants. Configurations with master and the latest stable branch are
# tested.
STABLE_BRANCH = sorted(
    filter(lambda b: b != 'master',
           map(lambda c: c.replace('.ini', ''),
               os.listdir(os.path.join(SRCDIR, 'config/branch'))))
)[-1]
RELEASE_TARGETS = [
    'eos-amd64-amd64-base',
    'eos-amd64-amd64-en',
    'eos-amd64-amd64-es',
    'eos-amd64-amd64-fr',
    'eos-amd64-amd64-pt_BR',
    'eos-arm64-rpi4-base',
    'eos-arm64-rpi4-en',
    'eos-arm64-pinebookpro-base',
    'eos-arm64-pinebookpro-en',
    'eos-arm64-vim2-base',
    'eos-arm64-vim2-en',
    'eos-arm64-libretechcc-base',
    'eos-arm64-libretechcc-en',
    'eosinstaller-amd64-amd64-base',
]
TEST_VARIANTS = []
for target in RELEASE_TARGETS:
    product, arch, platform, personality = target.split('-')
    TEST_VARIANTS += [
        (product, 'master', arch, platform, personality),
        (product, STABLE_BRANCH, arch, platform, personality),
    ]


@pytest.mark.parametrize('product,branch,arch,platform,personality',
                         TEST_VARIANTS)
def test_configure_variant(make_builder, product, branch, arch, platform,
                           personality):
    """Ensure variant configuration can be read, merged and validated"""
    builder = make_builder(product=product, branch=branch, arch=arch,
                           platform=platform, personality=personality)
    builder.configure()
    builder.check_config()

    # Make sure all values can be interpolated
    for section in builder.config.sections():
        for option, value in builder.config.items(section):
            logger.debug('%s:%s = %s', section, option,
                         value.replace('\n', ' '))
