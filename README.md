Endless OS Builder (EOB)
========================

This program assembles the Endless OS (EOS) from prebuilt packages
and content. Its main functions are:
 1. Assemble packages into ostree
 2. Create images from ostree with content added

Design
======

EOB is designed to be simple. It is written in bash script and has just
enough flexibility to meet our needs. The simplicity allows us to have a
complete in-house understanding of the build system, enabling smooth
organic growth as our requirements evolve. The build master(s) who maintain
this system are not afraid of encoding our requirements in simple bash script.

When added complexity is minimal, we prefer calling into lower level tools
directly rather than utilizing abstraction layers (e.g. we call debootstrap
instead of using live-tools). This means we have a thorough understanding of
the build system and helps to achieve our secondary goals of speed and
flexibility.

The build process is divided into several stages, detailed below. An
invocation can run some or all of these stages.

check_update stage
------------------

This stage does not perform image building, but is used to determine if
an image build is required. If it exits successfully, no image build is
needed.

OS stage
--------

This stage creates the OS in a clean directory tree, populating it with
apt packages.

ostree stage
------------

This stage makes appropriate modifications to the output of the previous
stage and commits it to a locally stored ostree repository. The ostree
repository is created if it does not already exist.

image stage
-----------

This stage checks out the latest ostree into a new directory, configures
the bootloader, adds content, and creates output images.
This stage has an internal loop which creates images for each personality.

split stage
-----------

This stage takes the newly created image file and creates a 2nd set of
images for use in a 2 disk setup. This is performed in a loop over
personalities.

publish stage
-------------

This stage publishes the ostree repository and final images to a place
where they can be accessed by developers/users.

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
 * python-apt
 * x86: grub2
 * arm: mkimage, device-tree-compiler

ostree signing
--------------

EOB signs the ostree commits it makes with GPG. A private keyring must be
installed in /etc/image-utils/gnupg and the key ID must be set in the
configuration.

Configuration
=============

The base configuration is kept in the run-build script. Some configuration
is static, and some is dynamic. Update this file directly when making changes
that are semi-permanent or permanent.

For one-off builds that require a different configuration, create a file
named config and put `export key=value` pairs there to override the
defaults. However, this file is sourced by the run-build script, so any
bash can be used to set the variables. Delete this file after the
one-off build has been made.

More permanent configuration changes can be kept in a system
configuration file at /etc/image-utils/config. This is sourced before
the local config file to allow it to override the system settings. The
same rules apply for the contents of the system config file.

Execution
=========

To run EOB, use the endless-os-builder script, optionally with a branch name:
 # ./endless-os-builder [options] master

If no branch name is specified, dev is used.
If you want to only run certain stages, modify the `buildscript` file
accordingly before starting the program.

Options available:
  --product : specify product to build (eos, eosdev)

Customization
=============

The core of EOB is just a wrapper. The real content of the output is defined
by customization scripts found under customization/. These scripts have
access to environment variables and library functions allowing them to
integrate correctly with the core.

The scripts for each product are kept under `customization/PRODUCT`.
`customization/all` is special, it is run on all builds.

If a script has an executable bit, it is executed directly. Otherwise it
is executed through bash and will have access to the library functions.

If a script filename finishes with ".chroot" then it is executed within
the chroot environment, as if it is running on the final system. Otherwise,
the script is executed under the regular host environment. It is preferred
to avoid chrooted scripts when it is easy to run the operation outside of
the chroot environment.

Scripts are executed in lexical order and the convention is to prefix them
with a two-digit number to make the order explicit. Each script should be
succinct - we prefer to have a decent number of small-ish scripts, rather
than having a small number of huge bash rambles.

check_update customization
--------------------------

The check_update stage calls the `cache` customization hooks. The
intention is to determine facts about the current build and compare them
to cached facts from the previous build. Facts are stored in the build
specific cache directory, determined from the function `eob_cachedir`.
Cache files should be named using the eob_cachefile function.

The check_update stage determines if an update is needed by seeing if
the modification times for any files in the cache directory have been
updated. Therefore, the hook should only update its cache file if
there's a difference from the previous build.

os customization
----------------

At the start of the os stage, the customization hooks under `seed` are run.
At this stage the `${INSTALL_ROOT}` is totally empty. Place anything here
that you want to be present at the time of initial bootstrap, which follows.

After the initial bootstrap, the customization hooks under `os` are run.
These scripts are responsible for making any configuration changes to the
system (discouraged), installing packages, etc. The OS packages are installed
by scripts at index 50.

image customization
-------------------

At the start of the image stage, the customization hooks under `content`
are run. These hooks are intended to ensure that all content for all
personalities is available on host disk, to be used later. `${EOB_CONTENT}`
should be used for storing this.

Once the ostree has been checked out (onto the host disk), customization
hooks under `image` are run, *once for each personality*.
`${OSTREE_DEPLOYMENT}` contains the path to the checkout, and
`${PERSONALITY}` states which personality is being built.

For reasons of speed, the ostree deployment is not recreated for each
personality. This means that *all customization scripts here should
unconditionally wipe out the results of previous runs* before making any
changes, otherwise changes from previous personalities might spill over
into the current one.

split customization
-------------------

Image splitting needs to occur for each full image file created, so the
`split` hooks are run *once for each personality*.
`${OSTREE_DEPLOYMENT}` contains the path to the checkout,
`${EXTRA_MOUNT}` contains the chroot-relative path to the extra storage
(currently `/var/endless-extra`), and `${PERSONALITY}` states which
personality is being built.

The ostree deployment /var is bind mounted at `${OSTREE_DEPLOYMENT}/var`
to resemble a real booted system. The 2nd disk filesystem is then
mounted at `${OSTREE_DEPLOYMENT}/${EXTRA_MOUNT}`. Hooks are intended to
migrate content from the root into this filesystem. The filesystem is a
fixed size (currenlty 8 GB), so hooks are required to ignore failures
due to insufficient space and revert to the original layout.

publish customization
---------------------

Keeping with the design that the core is simple and the meat is kept
under customization, the publish stage does nothing more than call
into customization hooks kept in `publish`. As publishing requirements
vary from host to host, we maintain a different script per each build host.

Each script should take the output of `${EOB_OUTDIR}` and push it to the
final destination, while also publishing the ostree from `${EOB_OSTREE}`.


error customization
---------------------

Like the publish stage, the error stage simply calls the customization
hooks kept in `error`. As publishing of build logs varies from host to
host, we maintain a different script per each build host.

Each script should take the `build.txt` file from `${EOB_OUTDIR}` and
push it to the final destination.
