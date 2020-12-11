# Tests for eib.sh

import eib
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
    expected_outdir = os.path.join(eib.CACHEDIR, 'tmp', 'out', personality)
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
