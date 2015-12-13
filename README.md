Endless Image Builder (EIB)
===========================

This program assembles the disk images for the Endless OS (EOS) from
OSTree filesystem trees. Its main functions are:

 1. Pull the latest OSTree filesystem tree
 2. Create images from ostree with content added

Design
======

EIB is designed to be simple. It is written in bash script and has just
enough flexibility to meet our needs. The simplicity allows us to have a
complete in-house understanding of the build system, enabling smooth
organic growth as our requirements evolve. The build master(s) who
maintain this system are not afraid of encoding our requirements in
simple bash script.

When added complexity is minimal, we prefer calling into lower level
tools directly rather than utilizing abstraction layers (e.g. we call
mke2fs instead of using live-tools). This means we have a thorough
understanding of the build system and helps to achieve our secondary
goals of speed and flexibility.

The build process is divided into several stages, detailed below. An
invocation can run some or all of these stages.

check_update stage
------------------

This stage does not perform image building, but is used to determine if
an image build is required. If it exits successfully, no image build is
needed.

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

Known to work on Debian Wheezy, Ubuntu 13.04 and Ubuntu 13.10.
Required packages:
 * pigz rsync
 * ostree
 * python3-apt
 * attr
 * x86: grub2
 * arm: mkimage, device-tree-compiler

Image signing
-------------

EIB signs the completed images with GPG. A private keyring must be
installed in /etc/eos-image-builder/gnupg and the key ID must be set in
the configuration.

Configuration
=============

The base configuration is kept in the run-build script. Some
configuration is static, and some is dynamic. Update this file directly
when making changes that are semi-permanent or permanent.

For one-off builds that require a different configuration, create a file
named config and put `export key=value` pairs there to override the
defaults. However, this file is sourced by the run-build script, so any
bash can be used to set the variables. Delete this file after the
one-off build has been made.

More permanent configuration changes can be kept in a system
configuration file at /etc/eos-image-builder/config. This is sourced
before the local config file to allow it to override the system
settings. The same rules apply for the contents of the system config
file.

Execution
=========

To run EIB, use the eos-image-builder script, optionally with a branch name:
 # ./eos-image-builder [options] master

If no branch name is specified, master is used. If you want to only run
certain stages, modify the `buildscript` file accordingly before
starting the program.

Options available:
  --product : specify product to build (eos, eosnonfree, eosdev)
  --platform : specify a sub-architecture to build (ec100, odroidu2)
  --personalities : specify image personaities to build (base, en)
  --force : perform a build even if the update check says it's not needed
  --dry-run : perform a build, but do not publish the results

Customization
=============

The core of EIB is just a wrapper. The real content of the output is
defined by customization scripts found under customization/. These
scripts have access to environment variables and library functions
allowing them to integrate correctly with the core.

The scripts for each product are kept under `customization/PRODUCT`.
`customization/all` is special, it is run on all builds.

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

check_update customization
--------------------------

The check_update stage calls the `cache` customization hooks. The
intention is to determine facts about the current build and compare them
to cached facts from the previous build. Facts are stored in the build
specific cache directory, determined from the function `eib_cachedir`.
Cache files should be named using the eib_cachefile function.

The check_update stage determines if an update is needed by seeing if
the modification times for any files in the cache directory have been
updated. Therefore, the hook should only update its cache file if
there's a difference from the previous build.

ostree customization
--------------------

The ostree stage currently has no customization hooks.

image customization
-------------------

At the start of the image stage, the customization hooks under `content`
are run. These hooks are intended to ensure that all content for the
current personality is available on host disk, to be used later.
`${EIB_CONTENTDIR}` should be used for storing this, and
`${EIB_PERSONALITY}` states which personality is being built.

Once the ostree has been checked out (onto the host disk), customization
hooks under `image` are run. `${OSTREE_DEPLOYMENT}` contains the path to
the checkout, and `${EIB_PERSONALITY}` states which personality is being
built.

After the full image file has been created, the `split` hooks to prepare
the 2 filesystems. `${OSTREE_DEPLOYMENT}` contains the path to the
checkout, `${EXTRA_MOUNT}` contains the chroot-relative path to the
extra storage (currently `/var/endless-extra`), and `${PERSONALITY}`
states which personality is being built.

The ostree deployment /var is bind mounted at `${OSTREE_DEPLOYMENT}/var`
to resemble a real booted system. The 2nd disk filesystem is then
mounted at `${OSTREE_DEPLOYMENT}/${EXTRA_MOUNT}`. Hooks are intended to
migrate content from the root into this filesystem. The filesystem is a
fixed size (currently 8 GB), so hooks are required to ignore failures
due to insufficient space and revert to the original layout.

publish customization
---------------------

Keeping with the design that the core is simple and the meat is kept
under customization, the publish stage does nothing more than call into
customization hooks kept in `publish`. These hooks should take the
output of `$(eib_outdir)` and push it to the final destination.

error customization
-------------------

Like the publish stage, the error stage simply calls the customization
hooks kept in `error`. These hooks should take the `build.txt` file from
`${EIB_OUTROOTDIR}` and push it to the final destination. This stage should
also clean up for subsequent builds.
