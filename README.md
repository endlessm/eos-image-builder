Endless Image Builder (EIB)
===========================

This program assembles the disk images for the Endless OS (EOS) from
OSTree filesystem trees. Its main functions are:

 1. Pull the latest OSTree filesystem tree
 2. Create images from ostree with content added

Design
======

EIB is designed to be simple but flexible. The core is written in bash
script and has just enough flexibility to our needs. The simplicity
allows us to have a complete in-house understanding of the build system,
enabling smooth organic growth as our requirements evolve. The build
master(s) who maintain this system are not afraid of encoding our
requirements in simple bash script.

The top level entry point is written in python. This is done to provide
a rich configuration environment that allows Endless to quickly adjust
to different product needs. See the [Configuration](#Configuration)
section below for a detailed description of the image builder
configuration.

When added complexity is minimal, we prefer calling into lower level
tools directly rather than utilizing abstraction layers (e.g. we call
mke2fs instead of using live-tools). This means we have a thorough
understanding of the build system and helps to achieve our secondary
goals of speed and flexibility.

The build process is divided into several stages, detailed below. An
invocation can run some or all of these stages.

ostree stage
------------

This stage pulls the latest tree from the remote ostree server to use as
the image base.

image stage
-----------

This stage checks out the latest ostree into a new directory, configures
the bootloader, adds content, and creates output images. After
completing the image, a 2nd set of images for use in a 2 disk setup is
created.

manifest stage
-------------

This stage assembles facts about the build and generates a merged JSON
file in the output directory for publishing.

publish stage
-------------

This stage does a final publishing of the output directory to the remote
image server.

error stage
-----------

This stage is only run in the event of an error and simply publishes the
build log to the image server.

Setup
=====

Known to work on Debian Buster (10) and newer. Required packages:

 * mmdebstrap
 * gnupg
 * python3
 * rsync

Image signing
-------------

EIB signs the completed images with GPG. A private keyring must be
installed in /etc/eos-image-builder/gnupg and the key ID must be set in
the configuration.

SSH authentication
------------------

EIB uses a private key at /etc/eos-image-builder/ssh-key.pem as the
identity file whenever ssh is used. SSH may be used for git fetching,
content downloading, or image publishing.

AWS authentication
------------------

EIB uses a shared AWS credentials file at
`/etc/eos-image-builder/aws-credentials` to authenticate to AWS services
such as S3. See the
[AWS documentation](http://docs.aws.amazon.com/cli/latest/userguide/cli-config-files.html)
for details on this file.

Netrc authentication
--------------------

Authentication credentials for various remote servers are stored in a
netrc(5) file at `/etc/eos-image-builder/netrc`. This can be passed to
`curl` or parsed with the `netrc` `python` module.

Configuration
=============

The image builder configuration is built up from a series of INI files.
The configuration files are stored in the `config` directory of the
builder. The order of configuration files read in is:

  * Default settings - `config/defaults.ini`
  * Product settings - `config/product/$product.ini`
  * Branch settings - `config/branch/$branch.ini`
  * Architecture settings - `config/arch/$arch.ini`
  * Platform settings - `config/platform/$platform.ini`
  * Personality settings - `config/personality/$personality.ini`

The image builder will then search for combinations of the build
attributes. For instance, a product and personality combination would be
read from `config/product-personality/$product-$personality.ini`. The
order and format of these files follows the order of the attributes
shown above. For instance, a branch and arch combination will always be
specified in the form `branch-arch` rather than `arch-branch`. Likewise,
this combination will come before a branch and platform combination.

System specific configuration is then loaded from
`/etc/eos-image-builder/config.ini`.

Next, a local tree of configuration can be loaded from
`$localdir/config` if a local settings directory is specified with the
`--localdir` option. The configuration loaded here is the same as in the
`config` directory in the source tree. For example,
`$localdir/config/defaults.ini` and
`$localdir/config/branch/$branch.ini` will be loaded.

For build specific settings, `config/local.ini` is then loaded from the
source tree.

Finally, there are 3 configuration files whose settings will be used
during the build but not displayed in the saved configuration file:

  * System private settings - `/etc/eos-image-builder/private.ini`
  * Local private settings = `$localdir/config/private.ini`
  * Checkout private settings - `config/private.ini`

None of these files are required to be present, but the `defaults.ini`
file contains many settings that are expected throughout the core of the
build.

New configuration options should be added and documented in
`defaults.ini`. See the existing file for options that are available to
customize. Settings in the `build` section are usually set in the
`ImageBuilder` class as they're static across all builds.

Format
------

The format of the configuration files is INI as mentioned above.
However, a form of interpolation is used to allow referring to other
options. For instance, an option `foo` can use the value from an option
`bar` by using `${bar}` in its value. If `bar` was in a different
section, it can be referred to by prepending the other section in the
form of `${other:bar}`.

The INI file parsing is done using the `configparser` `python` module.
The interpolation feature is provided by its `ExtendedInterpolation`
class. See the `python`
[documentation](https://docs.python.org/3/library/configparser.html#configparser.ExtendedInterpolation)
for a more detailed discussion of this feature.

The system and local configuration files are not typically used. They
can allow for a permanent or temporary override for a particular host or
build.

Schema
------

The image builder configuration scheme is intentionally very flexible to
allow building images with various combinations of attributes. That
flexibility means that it's easy to initiate builds with unsupported
settings. A configuration schema can be defined to help ensure specific
keys are set or to limit their values to a supported set.

The configuration schema is defined in `config/schema.ini` and
`$localdir/config/schema.ini`. Schema files are INI formatted like the
configuration itself. Sections and key names match the config files,
with key suffixes as follows:

 * `_required`: `true` means that the key must be set
 * `_values`: the value, if set, must be within the space-separated list
   of values here

Merged options
--------------

In some cases, an option needs to represent a set of values rather than
a single setting. Adding or removing items from the list is not possible
with the features in the configuration parser.

To allow some method of building these lists, the builder will take
options of the form `<option>_add*` and `<option>_del*` and merge them
together into one option named `<option>`. Each whitespace separated
value in the `add` and `del` variants is counted to determine whether it
will remain in the merged option value. A value found in `add` will have
its count incremented while a value found in `del` will have its count
decremented. If the final count is less than or equal to 0, it is
removed from the merged value.

Normally options loaded later in the configuration will override
identically named options from earlier in the configuration. If an
unmerged variant ends in `_add` or `_del`, a suffix based on the
filesystem path will automatically be appended to make it unique. For
instance, the option `packages_add` in `defaults.ini` will be converted
to `packages_add_defaults`, and the option `apps_add` in
`product/eos.ini` will be converted to `apps_add_product_eos`. These
options can be interpolated in other parts of the configuration using
the converted names.

Configuration files in the system directory will additionally include
`system` in the merged option. For example, the options `apps_del` in
`/etc/eos-image-builder/config.ini` will be converted to
`apps_del_system_config`. Alternatively, any unmerged option that
contains a suffix after `add` or `del` will be left as is such as
`apps_add_mandatory` in `defaults.ini`.

If the option `<option>` already exists, it is not changed. This allows
a configuration file to override all of the various `add` and `del`
options from other files to provide the list exactly in the form it
wants.

The current merged options are defined in the `ImageConfigParser` class
attribute `MERGED_OPTIONS` in the [eib](lib/eib.py) module. See the
`defaults.ini` file for a description of these options.

Accessing options
-----------------

The build core accesses these settings via environment variables. The
variables take the form of `EIB_$SECTION_$OPTION`. The `build` section
is special and these settings are exported in the form `EIB_$OPTION`
without the section in the variable name.

Seeing the full configuration
-----------------------------

In order to see what the full configuration will look like after merging
all configuration files and keys, run `./eos-image-builder
--show-config` with other `--product` type options for selecting the
appropriate image variant. This will print the merged configuration in
INI format. The merged configuration is also saved during the build into
the output directory.

Seeing the apps and runtimes
----------------------------

Sometimes you may want to see which apps and runtimes will be included in an
image, without actually building the image. To do this, run
`./eos-image-builder --show-apps` with other `--product` type options for
selecting the appropriate image variant. This will print tables of apps,
grouped by their runtime, along with compressed and uncompressed size estimates
for each app and runtime.

```
# ./eos-image-builder --show-apps --product eos --personality pt_BR eos3.4
```

If you want to group by regional-personality-specific vs generic vs runtime
instead, use `--group-by nature`:

```
# ./eos-image-builder --show-apps --group-by nature --product eos --personality pt_BR eos3.4
```

If you are trying to reduce the compressed image size by, say, 300 MB, you can
pass `--trim BYTES`, and see crude suggestions for which
apps to remove. (Hint: for images with a size limit, the number to use is in
the image build log.)

```
# ./eos-image-builder --show-apps --trim 300000000 --product eos --personality pt_BR eos3.4
```

Execution
=========

To run EIB, use the `eos-image-builder` script, optionally with a branch name:

```
# ./eos-image-builder [options] master
```

If no branch name is specified, master is used. If you want to only run
certain stages, modify the `buildscript` file accordingly before
starting the program.

Options available:

* `--product`: specify product to build (eos, eosnonfree, eosdev)
* `--platform`: specify a sub-architecture to build (e.g. pinebookpro)
* `--personality`: specify image personality to build (base, en)
* `--dry-run`: perform a build, but do not publish the results

Customization
=============

The core of EIB is just a wrapper. The real content of the output is
defined by customization scripts found under hooks/. These scripts have
access to environment variables and library functions allowing them to
integrate correctly with the core. If a local settings directory is
provided with the `--localdir` option, hooks in the `$localdir/hooks`
directory are preferred to those in the checkout's `hooks` directory.
This allows providing both custom hooks as well as overriding existing
hooks.

The scripts to run are organized under `hooks/GROUP` where `GROUP` is a
group of hooks run by a particular stage. The hooks to run are managed
in the configuration with merged `hooks` keys under each group. For
instance, the `image` group hooks to run are defined in the
`image:hooks` configuration key. This allows easy customization for
different OS variants. These are merged options as described above, so
they can be added to or pruned by specific products.

If a script has an executable bit, it is executed directly. Otherwise it
is executed through bash and will have access to the library functions.

If a script filename finishes with ".chroot" then it is executed within
the chroot environment, as if it is running on the final system.
Otherwise, the script is executed under the regular host environment. It
is preferred to avoid chrooted scripts when it is easy to run the
operation outside of the chroot environment.

Scripts are executed in lexical order and the convention is to prefix
them with a two-digit number to make the order explicit. Each script
should be succinct - we prefer to have a decent number of small-ish
scripts, rather than having a small number of huge bash rambles.

ostree customization
--------------------

The ostree stage currently has no customization hooks.

image customization
-------------------

At the start of the image stage, the customization hooks under `content`
are run. These hooks are intended to ensure that all content for the
current personality is available on the host disk, to be used later.
`${EIB_CONTENTDIR}` should be used for storing this, and
`${EIB_PERSONALITY}` states which personality is being built.

Once the ostree has been checked out (onto the host disk), customization
hooks under `image` are run. `${OSTREE_DEPLOYMENT}` contains the path to
the checkout, and `${EIB_PERSONALITY}` states which personality is being
built.

The entire cache (`${EIB_CACHEDIR}`) and source (`${EIB_SRCDIR}`)
directories are made available to both the `image` chroot hooks. This
includes other directories and files derived from the cache directory
such as `${EIB_CONTENTDIR}` and `${EIB_OUTDIR}` or source directory such
as `${EIB_DATADIR}` or `${EIB_HELPERSDIR}`.

Installation of Flatpaks is handled in the image stage. This uses
configuration in `flatpak-remote-*` sections. See the comments in
[default configuration file](config/defaults.ini) for details.

manifest customization
----------------------

The manifest stage takes fragment JSON files found in the
`${EIB_MANIFESTDIR}` directory and assembles them into a merged JSON
file in `${EIB_OUTDIR}`. Some basic facts about the build are provided,
but it's expected that other stages generate fragment JSON files as they
produce content. A `manifest` hook group is provided for customization
that is best done when the build is completed.

publish customization
---------------------

Keeping with the design that the core is simple and the meat is kept
under customization, the publish stage does nothing more than call into
customization hooks kept in `publish`. These hooks should take the
output of `${EIB_OUTDIR}` and push it to the final destination.

error customization
-------------------

Like the publish stage, the error stage simply calls the customization
hooks kept in `error`. These hooks should take the `build.txt` file from
`${EIB_OUTDIR}` and push it to the final destination. This stage should
also clean up for subsequent builds.

Testing
=======

Some parts of the image builder can be tested with [pytest][pytest-url].
After installing pytest, run `pytest` (or `pytest-3` if `pytest` is for
python 2) from the root of the checkout.

Various options can be passed to `pytest` to control how the tests are
run. See the pytest [usage][pytest-usage] documentation for details.
When debugging test failures, the `--basetemp` option allows specifying
the directory where each test's temporary directory will be stored. This
can be helpful to examine the files generated during tests. Another
useful option is `--log-cli-level=DEBUG`. Normally log messages are
suppressed unless there's a test failure. This option prints the
messages in the test output as they happen.

[pytest-url]: https://docs.pytest.org/en/stable/
[pytest-usage]: https://docs.pytest.org/en/stable/usage.html

The default image builder configuration and execution options are setup
for building production images on the Endless builders with access to
all needed assets. However, when making changes on the image builder,
it's important to test them locally. There are a few options available
for running the image builder locally in a mostly unprivileged
environment.

First, `eos-image-builder` provides options that are more appropriate
for testing. The `-n` or `--dry-run` option will skip publishing of the
completed image. This not only keeps the test image from being
published, but it avoids likely authentication errors with other Endless
services.

Next, `config/local.ini` can be used to change the image configuration
in various ways that make a local build more likely to succeed. Since
`local.ini` is parsed last, it can be used to override any other
settings. The file `config/local.ini.example` has examples of these
types of settings. Copying it to `config/local.ini` and making any
further local adjustments is recommended. See the comments in the
example file.

Finally, `eos-image-builder` uses private keys in
`/etc/eos-image-builder` to manage authentication and image signing. The
`local.ini.example` file sets various options so that authentication to
external services is generally not required. See the [Setup](#setup)
section above if you want to test authentication or GPG signing.

Now you should be able to run `sudo ./eos-image-builder` with the
options mentioned above as well as any `--product` type options to
select the appropriate image variant for the base configuration.

Licensing and redistribution
============================

Images built with this tool include Endless OS (more precisely: the Endless
OS ostree filesystem tree), a copyrighted collective work of the Endless OS
Foundation, and hence any redistribution of such images is subject to the
[Endless OS Redistribution Policy](https://endlessos.com/redistribution-policy/).

This eos-image-builder tool in itself is Open Source software licensed
under the GNU GPL v2.
