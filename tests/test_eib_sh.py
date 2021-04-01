# Tests for eib.sh

import eib
import hashlib
import os
import pytest
import subprocess
import sys
from textwrap import dedent

from .util import SRCDIR, TEST_KEY_IDS

EIB = os.path.join(SRCDIR, 'lib', 'eib.sh')


def run_lib(builder, script, check=True, env=None):
    """Run script after sourcing eib.sh"""
    full_script = '. {}\n{}\n'.format(EIB, script).encode('utf-8')
    cmd = ('/bin/bash', '-ex')
    build_env = builder.get_environment()
    if env:
        build_env.update(env)
    try:
        proc = subprocess.run(cmd, input=full_script, check=check,
                              env=build_env, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE)

        # Always dump stderr for test diagnosis
        sys.stderr.write(proc.stderr.decode('utf-8'))

        return proc
    except subprocess.CalledProcessError as err:
        sys.stderr.write(err.stderr.decode('utf-8'))
        raise


def run_lib_output(*args, **kwargs):
    return run_lib(*args, **kwargs).stdout.decode('utf-8').strip()


@pytest.mark.parametrize('variant', [
    ('eos-master-amd64-amd64-base'),
    ('eos-eos3.9-amd64-amd64-pt_BR'),
])
def test_eib_outfile(make_builder, variant):
    product, branch, arch, platform, personality = variant.split('-')
    builder = make_builder(product=product,
                           branch=branch,
                           arch=arch,
                           platform=platform,
                           personality=personality)
    builder.configure()
    expected_outdir = os.path.join(eib.CACHEDIR, 'tmp', 'out')
    expected_outversion = '{}-{}-{}-{}.{}.{}'.format(
        product, branch, arch, platform,
        builder.config['build']['build_version'], personality
    )
    expected = os.path.join(expected_outdir, expected_outversion + '.foo')
    script = 'eib_outfile foo'
    output = run_lib_output(builder, script)
    assert output == expected


def test_locale_to_iso(make_builder):
    """Test locale_to_iso_639_1 conversion"""
    builder = make_builder()

    cases = [
        ('en', 'en'),
        ('es_GT', 'es-GT'),
        ('br_FR.iso885915@euro', 'br-FR'),
    ]

    for locale, iso in cases:
        script = 'locale_to_iso_639_1 {}'.format(locale)
        output = run_lib_output(builder, script)
        assert output == iso


def test_eib_retry(make_builder):
    """Test eib_retry"""
    # Shorten the normal retry loop
    env = {
        'EIB_RETRY_ATTEMPTS': '2',
        'EIB_RETRY_INTERVAL': '0.1',
    }

    builder = make_builder()

    # Bad arguments
    proc = run_lib(builder, 'eib_retry', check=False, env=env)
    assert proc.returncode != 0
    assert ('error: No command supplied to eib_retry'
            in proc.stderr.decode('utf-8'))

    # Failing command
    proc = run_lib(builder, 'eib_retry false', check=False, env=env)
    assert proc.returncode != 0
    assert 'false failed; retrying...' in proc.stderr.decode('utf-8')
    assert 'false failed 2 times; giving up' in proc.stderr.decode('utf-8')

    # Ignored failing command
    proc = run_lib(builder, 'eib_retry false || true', env=env)
    assert 'false failed; retrying...' in proc.stderr.decode('utf-8')
    assert 'false failed 2 times; giving up' in proc.stderr.decode('utf-8')

    # Passing command
    proc = run_lib(builder, 'eib_retry true', env=env)
    assert 'retrying' not in proc.stderr.decode('utf-8')


def test_sign_file(make_builder, builder_gpgdir, tmp_path):
    builder = make_builder()
    builder.configure()

    test_file = tmp_path / 'test'
    test_file.write_text('test\n')
    test_sig = test_file.with_suffix('.asc')

    # Generate a keyring for verification
    keyring = tmp_path / 'keyring.gpg'
    subprocess.check_call(
        ('gpg', '--batch', '--homedir', str(builder_gpgdir),
         '--output', str(keyring), '--export', TEST_KEY_IDS['test1'])
    )

    # No file supplied is an error
    proc = run_lib(builder, 'sign_file', check=False)
    assert proc.returncode != 0
    assert 'No file supplied' in proc.stderr.decode('utf-8')

    # Null file supplied is an error
    proc = run_lib(builder, 'sign_file ""', check=False)
    assert proc.returncode != 0
    assert 'No file supplied' in proc.stderr.decode('utf-8')

    # With no signing key, no signature file should be made
    builder.config['image']['signing_keyid'] = ''
    script = 'sign_file {}'.format(test_file)
    run_lib(builder, script)
    assert not test_sig.exists()

    # Sign a file with the default output and verify it
    builder.config['image']['signing_keyid'] = TEST_KEY_IDS['test1']
    script = 'sign_file {}'.format(test_file)
    run_lib(builder, script)
    assert test_sig.exists()
    subprocess.check_call(
        ('gpgv', '--keyring', str(keyring), str(test_sig), str(test_file))
    )

    # Sign a file with a specified output and verify it
    builder.config['image']['signing_keyid'] = TEST_KEY_IDS['test1']
    sig_file = tmp_path / 'signature'
    script = 'sign_file {} {}'.format(test_file, sig_file)
    run_lib(builder, script)
    assert sig_file.exists()
    subprocess.check_call(
        ('gpgv', '--keyring', str(keyring), str(sig_file), str(test_file))
    )


def test_checksum_file(make_builder, tmp_path):
    builder = make_builder()
    builder.configure()

    test_file = tmp_path / 'test'
    contents = b'test\n'
    test_file.write_bytes(contents)
    test_csum = test_file.with_suffix('.sha256')
    expected_checksum = hashlib.sha256(contents).hexdigest()

    # No file supplied is an error
    proc = run_lib(builder, 'checksum_file', check=False)
    assert proc.returncode != 0
    assert 'No file supplied' in proc.stderr.decode('utf-8')

    # Null file supplied is an error
    proc = run_lib(builder, 'checksum_file ""', check=False)
    assert proc.returncode != 0
    assert 'No file supplied' in proc.stderr.decode('utf-8')

    # Sign a file with the default output and verify it
    script = 'checksum_file {}'.format(test_file)
    run_lib(builder, script)
    assert test_csum.exists()
    assert test_csum.read_text() == expected_checksum + '\n'

    # Sign a file with a specified output and verify it
    csum_file = tmp_path / 'checksum'
    script = 'checksum_file {} {}'.format(test_file, csum_file)
    run_lib(builder, script)
    assert csum_file.exists()
    assert csum_file.read_text() == expected_checksum + '\n'


def test_run_hooks(make_builder, tmp_bindir, tmp_path):
    """Test run_hooks"""
    builder = make_builder()
    builder.configure()
    builder.config.add_section('test')
    builder.config['build']['helpersdir'] = str(tmp_bindir)

    # Provide an eib-chroot that does basically nothing
    eib_chroot = tmp_bindir / 'eib-chroot'
    eib_chroot.touch(mode=0o777)
    eib_chroot.write_text(dedent("""\
    #!/bin/bash -ex
    echo In eib-chroot
    shift
    exec "$@"
    """))

    hooksdir = tmp_path / 'hooks'
    test_hooksdir = hooksdir / 'test'
    test_hooksdir.mkdir(parents=True)
    hook_name = '50-test'
    hook_path = test_hooksdir / hook_name
    chroot_hook_name = '50-test.chroot'
    chroot_hook_path = test_hooksdir / chroot_hook_name

    env = {
        'EIB_HOOKSDIR': str(hooksdir),
    }

    # Running hooks for a group where the hooks variable is unset or
    # empty should succed with no hooks run
    output = run_lib_output(builder, 'run_hooks test', env=env)
    assert 'Run hook' not in output
    builder.config['test']['hooks'] = ''
    output = run_lib_output(builder, 'run_hooks test', env=env)
    assert 'Run hook' not in output

    # Missing hook should fail
    builder.config['test']['hooks'] = str(hook_name)
    proc = run_lib(builder, 'run_hooks test', env=env, check=False)
    assert proc.returncode != 0
    assert 'Missing hook' in proc.stderr.decode('utf-8')

    # Non-executable sourced hook. This should be run in a subshell of
    # the the main bash process.
    hook_path.write_text(dedent("""\
    echo BASH_SUBSHELL=$BASH_SUBSHELL
    """))
    hook_path.chmod(0o644)
    builder.config['test']['hooks'] = str(hook_name)
    output = run_lib_output(builder, 'run_hooks test', env=env)
    assert 'BASH_SUBSHELL=1' in output

    # Executable hook
    hook_path.write_text(dedent("""\
    #!/usr/bin/env python3
    print('In python')
    """))
    hook_path.chmod(0o755)
    builder.config['test']['hooks'] = str(hook_name)
    output = run_lib_output(builder, 'run_hooks test', env=env)
    assert 'In python' in output

    # A chroot hook without the specifying the root in the run_hooks
    # call will be skipped.
    chroot_hook_path.touch(mode=0o644)
    builder.config['test']['hooks'] = str(chroot_hook_name)
    output = run_lib_output(builder, 'run_hooks test', env=env)
    assert 'Skipping hook, no chroot available' in output

    # Non-executable chroot hook. This should be run in bash and would
    # fail if the BASH_VERSION variable isn't set.
    chroot_hook_path.write_text(dedent("""\
    test -v BASH_VERSION
    echo "In bash"
    """))
    chroot_hook_path.chmod(0o644)
    builder.config['test']['hooks'] = str(chroot_hook_name)
    output = run_lib_output(builder, 'run_hooks test /', env=env)
    assert 'In eib-chroot' in output
    assert 'In bash' in output

    # Executable chroot hook
    chroot_hook_path.write_text(dedent("""\
    #!/usr/bin/env python3
    print('In python')
    """))
    chroot_hook_path.chmod(0o755)
    builder.config['test']['hooks'] = str(chroot_hook_name)
    output = run_lib_output(builder, 'run_hooks test /', env=env)
    assert 'In eib-chroot' in output
    assert 'In python' in output


def test_local_hooks(make_builder, tmp_path):
    """Test run_hooks with localdir hooks"""
    # Populate local and source hooks
    hooksdir = tmp_path / 'hooks'
    test_hooksdir = hooksdir / 'test'
    test_hooksdir.mkdir(parents=True)
    localdir = tmp_path / 'local'
    localhooksdir = localdir / 'hooks'
    test_localhooksdir = localhooksdir / 'test'
    test_localhooksdir.mkdir(parents=True)
    hook_name = '50-test'
    hook_path = test_hooksdir / hook_name
    hook_path.write_text(dedent("""\
    echo source hook
    """))
    hook_path.chmod(0o644)
    local_hook_path = test_localhooksdir / hook_name
    local_hook_path.write_text(dedent("""\
    echo local hook
    """))
    local_hook_path.chmod(0o644)

    env = {
        'EIB_HOOKSDIR': str(hooksdir),
    }

    # With no local directory specified, the source hook should be run
    builder = make_builder()
    builder.configure()
    builder.config.add_section('test')
    builder.config['test']['hooks'] = str(hook_name)
    output = run_lib_output(builder, 'run_hooks test', env=env)
    assert 'source hook' in output

    # With local directory specified, the local hook should be run
    builder = make_builder(localdir=str(localdir))
    builder.configure()
    builder.config.add_section('test')
    builder.config['test']['hooks'] = str(hook_name)
    output = run_lib_output(builder, 'run_hooks test', env=env)
    assert 'local hook' in output

    # With only the hook in the local directory, the local hook should
    # still run
    hook_path.unlink()
    builder = make_builder(localdir=str(localdir))
    builder.configure()
    builder.config.add_section('test')
    builder.config['test']['hooks'] = str(hook_name)
    output = run_lib_output(builder, 'run_hooks test', env=env)
    assert 'local hook' in output

    # With only the hook in the local directory but no local directory
    # specified, no hook should be run
    builder = make_builder()
    builder.configure()
    builder.config.add_section('test')
    builder.config['test']['hooks'] = str(hook_name)
    proc = run_lib(builder, 'run_hooks test', env=env, check=False)
    assert proc.returncode != 0
    assert 'Missing hook' in proc.stderr.decode('utf-8')

    # With neither hook available, it should fail whether a local
    # directory is supplied or not
    local_hook_path.unlink()
    builder = make_builder()
    builder.configure()
    builder.config.add_section('test')
    builder.config['test']['hooks'] = str(hook_name)
    proc = run_lib(builder, 'run_hooks test', env=env, check=False)
    assert proc.returncode != 0
    assert 'Missing hook' in proc.stderr.decode('utf-8')

    builder = make_builder(localdir=str(localdir))
    builder.configure()
    builder.config.add_section('test')
    builder.config['test']['hooks'] = str(hook_name)
    proc = run_lib(builder, 'run_hooks test', env=env, check=False)
    assert proc.returncode != 0
    assert 'Missing hook' in proc.stderr.decode('utf-8')
