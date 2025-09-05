# Tests for ImageBuilder class

import configparser
import eib
import logging
import os
import pytest
from textwrap import dedent

from .util import SRCDIR, import_script

run_build = import_script('run_build', os.path.join(SRCDIR, 'run-build'))

logger = logging.getLogger(__name__)


def test_config_attrs(make_builder):
    builder = make_builder()
    cases = [
        ('product', 'eoscustom', 'eoscustom'),
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


def test_get_environment(make_builder):
    builder = make_builder()

    builder.config.add_section('sect')
    builder.config['sect']['opt'] = 'a\n\tb'

    cases = [
        ('EIB_PRODUCT', 'eoscustom'),
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

    environ = builder.get_environment()
    for envvar, value in cases:
        assert envvar in environ
        assert environ[envvar] == value


@pytest.mark.parametrize('branch,expected', [
    ('master', 'master'), ('eos3.8', 'eos3'), ('eos3', 'eos3'),
    ('eos2.4', 'eos2'),
])
def test_series(make_builder, branch, expected):
    builder = make_builder(branch=branch)
    assert builder.series == expected


@pytest.mark.parametrize('arch,platform,expected', [
    ('amd64', None, 'amd64'),
    ('arm64', 'rpi4', 'rpi4'),
    ('arm64', None, 'arm64'),
    ('i386', 'i386', 'i386'),
    ('i386', None, 'i386'),
])
def test_platform(make_builder, arch, platform, expected):
    builder = make_builder(arch=arch, platform=platform)
    assert builder.platform == expected


def test_bad_arch(make_builder):
    with pytest.raises(eib.ImageBuildError,
                       match='Architecture.*not supported'):
        make_builder(arch='notanarch')


# Build test variants. This is ideally the same as the release image
# variants + some typical custom variants. Configurations with master
# and the latest stable branch are tested.
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
    'eosinstaller-amd64-amd64-base',
]
CUSTOM_TARGETS = [
    'eoscustom-amd64-amd64-base',
    'eoscustom-arm64-rpi4-base',
]
TEST_VARIANTS = []
for target in RELEASE_TARGETS + CUSTOM_TARGETS:
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


def test_config_paths(make_builder, tmp_path, tmp_builder_paths, caplog):
    """Test loading of various config paths"""
    expected_loaded = []
    expected_not_loaded = []
    expected_loaded_private = []
    expected_apps = []
    expected_signing_key = ''

    def _run_test():
        caplog.clear()
        builder = make_builder(localdir=str(localdir),
                               configdir=str(configdir))
        builder.configure()

        for path in expected_loaded:
            assert ('Loaded configuration file {}'.format(path)
                    in caplog.text)
        for path in expected_not_loaded:
            assert ('Loaded configuration file {}'.format(path)
                    not in caplog.text)
        for path in expected_loaded_private:
            assert ('Loaded private configuration file {}'.format(path)
                    in caplog.text)
        assert builder.config['flatpak-remote-eos-apps']['apps'] == expected_apps
        assert builder.config['image']['signing_key'] == expected_signing_key

    configdir = tmp_path / 'config'
    localdir = tmp_path / 'local'
    local_configdir = localdir / 'config'

    # Config defaults
    defaults = configdir / 'defaults.ini'
    defaults.parent.mkdir()
    defaults.write_text(dedent("""\
    [flatpak-remote-eos-apps]
    apps_add = a

    [image]
    signing_key = abcdefgh
    """))
    expected_loaded.append(defaults)
    expected_apps = 'a'
    expected_signing_key = 'abcdefgh'

    _run_test()

    # Local defaults
    local_defaults = local_configdir / 'defaults.ini'
    local_defaults.parent.mkdir(parents=True, exist_ok=True)
    local_defaults.write_text(dedent("""\
    [image]
    signing_key = ijklmnop
    """))
    expected_loaded.append(local_defaults)
    expected_apps = 'a'
    expected_signing_key = 'ijklmnop'

    _run_test()

    # Product config
    eoscustom = configdir / 'product' / 'eoscustom.ini'
    eoscustom.parent.mkdir(exist_ok=True)
    eoscustom.write_text(dedent("""\
    [flatpak-remote-eos-apps]
    apps_add = b
    apps_del = a
    """))
    expected_apps = 'b'
    expected_loaded.append(eoscustom)

    other = configdir / 'product' / 'other.ini'
    other.parent.mkdir(exist_ok=True)
    other.write_text(dedent("""\
    [flatpak-remote-eos-apps]
    apps_add = c
    """))
    expected_not_loaded.append(other)

    _run_test()

    # Local personality
    local_base = local_configdir / 'personality' / 'base.ini'
    local_base.parent.mkdir(exist_ok=True)
    local_base.write_text(dedent("""\
    [flatpak-remote-eos-apps]
    apps_del = e
    """))
    expected_loaded.append(local_base)

    local_other = local_configdir / 'personality' / 'other.ini'
    local_other.parent.mkdir(exist_ok=True)
    local_other.write_text(dedent("""\
    [flatpak-remote-eos-apps]
    apps_add = g
    """))
    expected_not_loaded.append(local_other)

    # Product-arch config
    eoscustom_amd64 = configdir / 'product-arch' / 'eoscustom-amd64.ini'
    eoscustom_amd64.parent.mkdir(exist_ok=True)
    eoscustom_amd64.write_text(dedent("""\
    [flatpak-remote-eos-apps]
    apps_add = d e
    """))
    expected_loaded.append(eoscustom_amd64)
    expected_apps = 'b\nd'

    _run_test()

    # System config file
    sysconfig = tmp_builder_paths['SYSCONFDIR'] / 'config.ini'
    sysconfig.parent.mkdir(exist_ok=True)
    sysconfig.write_text(dedent("""\
    [flatpak-remote-eos-apps]
    apps_add = f
    """))
    expected_loaded.append(sysconfig)
    expected_apps = 'b\nd\nf'

    _run_test()

    # Local checkout config
    checkout = configdir / 'local.ini'
    checkout.parent.mkdir(exist_ok=True)
    checkout.write_text(dedent("""\
    [flatpak-remote-eos-apps]
    apps = z
    """))
    expected_loaded.append(checkout)
    expected_apps = 'z'

    _run_test()

    # System private config file
    sysprivate = tmp_builder_paths['SYSCONFDIR'] / 'private.ini'
    sysprivate.parent.mkdir(exist_ok=True)
    sysprivate.write_text(dedent("""\
    [image]
    signing_key = 12345678
    """))
    expected_loaded_private.append(sysprivate)
    expected_signing_key = '12345678'

    _run_test()


def test_config_inheritance(make_builder, tmp_path, tmp_builder_paths, caplog):
    """Test that personality en_GB_orkney also loads settings from en and en_GB."""
    configdir = tmp_path / 'config'
    personalitydir = configdir / 'personality'
    personalitydir.mkdir(parents=True, exist_ok=True)

    en = personalitydir / 'en.ini'
    en.write_text(dedent("""\
    [image]
    language = en_US.utf8

    [flatpak-remote-eos-apps]
    apps_add =
      com.endlessm.encyclopedia.en
      com.endlessm.football.en
    """))

    en_GB = personalitydir / 'en_GB.ini'
    en_GB.write_text(dedent("""\
    [image]
    language = en_GB.utf8

    [flatpak-remote-eos-apps]
    # Football means something else in the UK
    apps_del =
      com.endlessm.football.en
    apps_add =
      com.endlessm.football.en_GB
    """))

    en_GB_orkney = personalitydir / 'en_GB_orkney.ini'
    en_GB_orkney.write_text(dedent("""\
    [flatpak-remote-eos-apps]
    apps_add =
      com.endlessm.orkneyingasaga
    """))

    builder = make_builder(configdir=str(configdir), personality="en_GB_orkney")
    builder.configure()

    assert builder.config['image']['language'] == "en_GB.utf8"
    assert builder.config['flatpak-remote-eos-apps']['apps'] == "\n".join([
      "com.endlessm.encyclopedia.en",
      "com.endlessm.football.en_GB",
      "com.endlessm.orkneyingasaga",
    ])


def test_localdir(make_builder, tmp_path, tmp_builder_paths, caplog):
    """Test use of local settings directory"""
    # Build without localdir
    builder = make_builder()
    builder.configure()
    environ = builder.config.get_environment()
    assert builder.localdir is None
    assert 'localdir' not in builder.config['build']
    assert 'EIB_LOCALDIR' not in environ

    # Build with configuration referencing localdir should raise an
    # exception
    sysconfig = tmp_builder_paths['SYSCONFDIR'] / 'config.ini'
    sysconfig.parent.mkdir(exist_ok=True)
    sysconfig.write_text(dedent("""\
    [image]
    branding_desktop_logo = ${build:localdatadir}/desktop.png
    """))
    caplog.clear()
    builder = make_builder()
    builder.configure()
    with pytest.raises(configparser.InterpolationMissingOptionError,
                       match='Bad value substitution'):
        builder.config['image']['branding_desktop_logo']

    # Build with localdir provided
    localdir = tmp_path / 'local'
    defaults = localdir / 'config' / 'defaults.ini'
    defaults.parent.mkdir(parents=True)
    defaults.write_text(dedent("""\
    [image]
    signing_key = foobar
    """))
    caplog.clear()
    builder = make_builder(localdir=str(localdir))
    builder.configure()
    environ = builder.config.get_environment()
    assert builder.localdir == str(localdir)
    assert builder.config['build']['localdir'] == str(localdir)
    assert builder.config['build']['localdatadir'] == str(localdir / 'data')
    assert environ['EIB_LOCALDIR'] == str(localdir)
    assert environ['EIB_LOCALDATADIR'] == str(localdir / 'data')
    assert 'Loaded configuration file {}'.format(defaults) in caplog.text
    assert (builder.config['image']['branding_desktop_logo'] ==
            str(localdir / 'data' / 'desktop.png'))
    assert builder.config['image']['signing_key'] == 'foobar'


def test_path_validation(make_builder, tmp_path):
    configdir = tmp_path / 'config'
    configdir.mkdir()

    # Schema
    schema = configdir / 'schema.ini'
    schema.write_text(dedent("""\
    [image]
    singular_type = path
    plural_type = paths
    """))

    # Some config
    defaults = configdir / 'defaults.ini'
    defaults.write_text(dedent("""\
    [image]
    singular = ${build:localdatadir}/a.txt
    plural = ${build:localdatadir}/b.txt ${build:localdatadir}/c.txt
    """))

    localdir = tmp_path / 'local'
    localdatadir = localdir / 'data'
    localdatadir.mkdir(parents=True)

    a = localdatadir / 'a.txt'
    a.touch()

    b = localdatadir / 'b.txt'
    b.touch()

    c = localdatadir / 'c.txt'
    c.touch()

    builder = make_builder(configdir=str(configdir), localdir=str(localdir))
    builder.configure()

    # All paths exist
    builder.check_config()

    b.unlink()
    with pytest.raises(eib.ImageBuildError, match=r'plural.*b.txt'):
        builder.check_config()

    b.touch()
    a.unlink()
    with pytest.raises(eib.ImageBuildError, match=r'singular.*a.txt'):
        builder.check_config()
