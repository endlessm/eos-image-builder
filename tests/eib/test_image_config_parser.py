# Tests for ImageConfigParser

import eib
import io
import os
import pytest
from textwrap import dedent

from ..util import SRCDIR


def get_combined_ini(config):
    """Get the combined config as a string"""
    with io.StringIO() as buf:
        config.write(buf)
        return buf.getvalue()


def test_missing(tmp_path, config):
    """Test reading missing config file

    ConfigParser succeeds if the path doesn't exist, which happens a lot
    because config files for all attributes of the build
    (product/arch/etc) are read.
    """
    assert not config.read_config_file(tmp_path / 'missing', 'missing')
    assert config.sections() == ['build']
    assert get_combined_ini(config) == '[build]\n\n'


def test_interpolation(tmp_path, config):
    """Test value interpolation"""
    f = tmp_path / 'f.ini'
    f.write_text(dedent("""\
    [build]
    a = a
    b = b

    [a]
    a = b
    c = c

    d = ${a}
    e = ${a:a}
    f = ${build:a}
    g = ${build:b}
    h = ${c}
    i = ${d}
    """))
    assert config.read_config_file(f, 'f')

    sect = config['a']
    assert sect['d'] == 'b'
    assert sect['e'] == 'b'
    assert sect['f'] == 'a'
    assert sect['g'] == 'b'
    assert sect['h'] == 'c'
    assert sect['i'] == 'b'


def test_config_multiple(tmp_path, config):
    """Test multipile config files combined"""
    f1 = tmp_path / 'f1.ini'
    f1.write_text(dedent("""\
    [a]
    a = a
    b = b
    """))

    f2 = tmp_path / "f2.ini"
    f2.write_text(dedent("""\
    [a]
    b = c
    c = c

    [b]
    a = a
      b
    """))

    assert config.read_config_file(f1, 'f1')
    assert config.read_config_file(f2, 'f2')

    assert config.sections() == ['build', 'a', 'b']
    assert config.options('a') == ['a', 'b', 'c']
    assert config.options('b') == ['a']
    assert config['a']['a'] == 'a'
    assert config['a']['b'] == 'c'
    assert config['a']['c'] == 'c'
    assert config['b']['a'] == 'a\nb'

    expected_combined = dedent("""\
    [build]

    [a]
    a = a
    b = c
    c = c

    [b]
    a = a
    \tb

    """)
    assert get_combined_ini(config) == expected_combined


def test_merged_option(config):
    """Test option merging"""
    config.MERGED_OPTIONS = [('sect', 'opt')]
    config.add_section('sect')

    # Standard add/del counters
    sect = config['sect']
    sect['opt_add_1'] = 'foo bar baz'
    sect['opt_add_2'] = 'baz'
    sect['opt_del_1'] = 'bar baz'
    config.merge()

    assert set(sect) == {'opt'}

    # The values will be sorted and newline separated
    assert sect['opt'] == 'baz\nfoo'

    # Now that the merged option exists, it will override any further
    # add/del.
    sect['opt_add_1'] = 'bar'
    sect['opt_del_1'] = 'foo'
    config.merge()

    assert set(sect) == {'opt'}
    assert sect['opt'] == 'baz\nfoo'


def test_merged_option_interpolation(config):
    """Test option merging with interpolation"""
    config.MERGED_OPTIONS = [('sect', 'opt')]
    config.add_section('sect')

    sect = config['sect']
    sect['opt_add_1'] = 'foo'
    sect['opt_add_2'] = 'bar baz'
    sect['opt_del_1'] = '${opt_add_1} baz'
    config.merge()

    assert set(sect) == {'opt'}

    assert sect['opt'] == 'bar'


def test_merged_pattern_section(config):
    """Test option merging in a patterned section"""
    config.MERGED_OPTIONS = [('sect-*', 'opt')]
    config.add_section('sect-a')
    a = config['sect-a']
    config.add_section('sect-b')
    b = config['sect-b']

    a['opt_add_test'] = 'foo\n  bar'
    a['opt_del_test'] = 'bar'
    b['opt'] = 'baz'
    b['opt_add_test'] = 'foo'
    config.merge()

    assert 'opt_add_test' not in a
    assert 'opt_del_test' not in a
    assert a['opt'] == 'foo'
    assert 'opt_add_test' not in b
    assert b['opt'] == 'baz'


def test_merged_namespace_errors(tmp_path, config):
    a = tmp_path / 'a.ini'
    b = tmp_path / 'b.ini'

    # Bad namespace options
    with pytest.raises(eib.ImageBuildError,
                       match='namespace must be a non-empty string'):
        config.read_config_file(a, None)
    with pytest.raises(eib.ImageBuildError,
                       match='namespace must be a non-empty string'):
        config.read_config_file(a, '')

    # Conflicting namespaces
    config.read_config_file(a, 'test')
    with pytest.raises(eib.ImageBuildError,
                       match='namespace test is already used'):
        config.read_config_file(b, 'test')


def test_merged_namespaces(tmp_path, config):
    config.MERGED_OPTIONS = [('sect', 'opt')]
    expected_opts = set()

    # Automatic namespacing
    a = tmp_path / 'a.ini'
    a.write_text(dedent("""\
    [sect]
    opt_add = foo
    opt_del = bar
    """))
    assert config.read_config_file(a, 'a')
    add_opt = 'opt_add_a'
    del_opt = 'opt_del_a'
    expected_opts.update({add_opt, del_opt})
    assert set(config['sect']) == expected_opts
    assert config['sect'][add_opt] == 'foo'
    assert config['sect'][del_opt] == 'bar'

    # Configuration defined namespacing
    b = tmp_path / 'b.ini'
    b.write_text(dedent("""\
    [sect]
    opt_add_test = bar
    opt_del_test = foo
    """))
    assert config.read_config_file(b, 'b')
    add_opt = 'opt_add_test'
    del_opt = 'opt_del_test'
    expected_opts.update({add_opt, del_opt})
    assert set(config['sect']) == expected_opts
    assert config['sect'][add_opt] == 'bar'
    assert config['sect'][del_opt] == 'foo'

    config.merge()
    assert set(config['sect']) == {'opt'}
    assert config['sect']['opt'] == ''


def test_merged_files(tmp_path, config):
    """Test option merging from files"""
    config.MERGED_OPTIONS = [('sect', 'opt'), ('sect-*', 'opt')]
    a = tmp_path / 'a.ini'
    a.write_text(dedent("""\
    [sect]
    opt_add =
      foo
      bar
      baz

    [sect-a]
    opt_add =
      foo
      bar

    [sect-b]
    opt = baz
    """))

    b = tmp_path / 'b.ini'
    b.write_text(dedent("""\
    [sect]
    opt_add =
      baz

    [sect-a]
    opt_del = bar

    [sect-b]
    opt_add = foo
    """))

    c = tmp_path / 'c.ini'
    c.write_text(dedent("""\
    [sect]
    opt_del =
      bar
      baz

    [sect-a]
    opt_del = foo
    """))

    assert config.read_config_file(a, 'a')
    assert config.read_config_file(b, 'b')
    assert config.read_config_file(c, 'c')
    config.merge()

    assert set(config['sect']) == {'opt'}
    assert config['sect']['opt'] == 'baz\nfoo'
    assert set(config['sect-a']) == {'opt'}
    assert config['sect-a']['opt'] == ''
    assert set(config['sect-b']) == {'opt'}
    assert config['sect-b']['opt'] == 'baz'


def test_defaults(builder_config):
    """Test defaults.ini can be loaded and resolved"""
    defaults = os.path.join(SRCDIR, 'config/defaults.ini')
    assert builder_config.read_config_file(defaults, 'defaults')
    for sect in builder_config:
        # Make sure all the values can be resolved
        builder_config.items(sect)


def test_all_current():
    """Test all current files can be loaded successfully"""
    src_configdir = os.path.join(SRCDIR, 'config')
    for cur, dirs, files in os.walk(src_configdir):
        for name in files:
            if not name.endswith('.ini'):
                continue
            if name == 'local.ini':
                continue
            path = os.path.join(cur, name)
            config = eib.ImageConfigParser()
            assert config.read_config_file(path, path.replace('/', '_'))
            assert get_combined_ini(config) != ''
