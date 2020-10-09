#!/bin/bash
# -*- mode: Shell-script; sh-basic-offset: 2; indent-tabs-mode: nil -*-
shopt -s nullglob
shopt -s extglob

# Make sure ERR traps follow through to shell functions
set -E

# Show current script and time when xtrace (set -x) enabled. Will look
# like "+ 10:13:40 run-build: some command".
export PS4='+ \t ${BASH_SOURCE[0]##*/}: '

# Exit code indicating new build needed rather than error
EIB_CHECK_EXIT_BUILD_NEEDED=90

# Runs a command chrooted with our helper script.
chroot () {
  "${EIB_HELPERSDIR}"/eib-chroot "$@"
}

# Run hooks under customization/
run_hooks() {
  local hook interpreter
  local group=$1
  local install_root=$2

  echo "Running $group hooks"
  export EIB_HOOK_GROUP=$group

  # Sort enabled hooks
  eval local hooks="\${EIB_${group^^}_HOOKS}"
  local files=$(echo "${hooks}" | tr ' ' '\n' | sort)

  for hook in ${files}; do
    local hookpath="${EIB_SRCDIR}"/hooks/${group}/${hook}
    if [ ! -f "${hookpath}" ]; then
      echo "Missing hook ${hookpath}!" >&2
      return 1
    fi

    if [ "${hook: -7}" == ".chroot" ]; then
      if [ -z "$install_root" ]; then
        echo "Skipping hook, no chroot available: ${hook}"
        continue
      fi

      echo "Run hook in chroot: ${hook}"
      [ -x "${hookpath}" ] && interpreter= || interpreter="bash -ex"
      mkdir -p $install_root/tmp
      cp ${hookpath} $install_root/tmp/${hook}
      chroot $install_root $interpreter /tmp/${hook}
      rm -f $install_root/tmp/${hook}
      continue
    fi

    echo "Run hook: ${hook}"

    if [ -x "${hookpath}" ]; then
      ${hookpath}
    else
      (
        . ${hookpath}
      )
    fi
  done
}

# Generate full path to output file
eib_outfile() {
  echo ${EIB_OUTDIR}/${EIB_OUTVERSION}.$1
}

# Encode the original image version as an xattr of the root directory of
# each partition.
# Usage: <root directory path>
eib_write_version_xattr() {
  attr -s eos-image-version -V "${EIB_OUTVERSION}" "$1"
}

# Declare the EIB_MOUNTS array, but don't reinitialize it.
declare -a EIB_MOUNTS

# Mount a filesystem and track the target mount point.
eib_mount() {
  local target

  if [ $# -lt 2 ]; then
    echo "At least 2 arguments needed to $FUNCNAME" >&2
    return 1
  fi

  # The target is the last argument
  eval target="\${$#}"

  mkdir -p "${target}"
  mount "$@"

  EIB_MOUNTS+=("${target}")
}

# Unmount a mount point and remove it from tracking.
eib_umount() {
  local target=${1:?No mount target supplied to $FUNCNAME}
  local -i n
  local -a new_mounts=()
  local found=false

  for mntpnt in "${EIB_MOUNTS[@]}"; do
    if [ "${mntpnt}" = "${target}" ]; then
      umount "${target}"
      found=true
    else
      # Build a new array with the remaining mounts
      new_mounts+=("${mntpnt}")
    fi
  done

  if $found; then
    # Assign the array to the new filtered version
    EIB_MOUNTS=("${new_mounts[@]}")
  else
    echo "Mount point ${target} not tracked in: ${EIB_MOUNTS[@]}" >&2
    return 1
  fi
}

# Unmount all tracked mount points.
eib_umount_all() {
  local -i n

  # Work from the end of the array to unmount submounts first
  for ((n = ${#EIB_MOUNTS[@]} - 1; n >= 0; n--)); do
    umount "${EIB_MOUNTS[n]}"
  done

  # Clear and re-declare the array
  unset EIB_MOUNTS
  declare -a EIB_MOUNTS
}

eib_fix_boot_checksum() {
  local disk=${1:?No disk supplied to ${FUNCNAME}}
  local deploy=${2:?No deployment supplied to ${FUNCNAME}}

  [ -x "${deploy}"/usr/sbin/amlogic-fix-spl-checksum ] || return 0
  "${deploy}"/usr/sbin/amlogic-fix-spl-checksum "${disk}"
}

# Work around transient failures
eib_retry() {
  local subcommand=${1:?No subcommand supplied to ${FUNCNAME}}
  local i=0
  local max_retries=10

  while ! "$@" && (( ++i < max_retries )) ; do
    echo "$@ failed; retrying..."
    sleep 1
  done

  if (( i >= max_retries )); then
    echo "$@ failed ${max_retries} times; giving up"
    exit 1
  fi
}

# Helpers for partx and losetup to work around races with device
# activity that cause the commands to fail with EBUSY.
eib_partx_scan() {
  udevadm settle
  eib_retry partx -a -v "$1"
}

eib_partx_delete() {
  udevadm settle
  eib_retry partx -d -v "$1"
}

eib_delete_loop() {
  udevadm settle
  eib_retry losetup -d "$1"
}

recreate_dir() {
  rm -rf $1
  mkdir -p $1
}

# Removes modifier, codeset and replace separator, so
# that a code like br_FR.iso885915@euro becomes br-FR
locale_to_iso_639_1() {
    local no_modifier=$(echo "${1}" | cut -d '@' -f1)
    local no_codeset=$(echo "${no_modifier}" | cut -d '.' -f1)
    echo "${no_codeset}" | sed s/'_'/'-'/
}

# Read ID of named user account from ostree deployment
ostree_uid() {
  grep ^${1}: ${OSTREE_DEPLOYMENT}/lib/passwd | cut -d : -f 3
}

# Read ID of named group from ostree deployment
ostree_gid() {
  grep ^${1}: ${OSTREE_DEPLOYMENT}/lib/group | cut -d : -f 3
}

# Created a detached signature with gpg.
sign_file() {
  local file=${1:?No file supplied to ${FUNCNAME}}
  local outfile=${2:-${file}.asc}

  if [ -n "${EIB_IMAGE_SIGNING_KEYID}" ]; then
    gpg --homedir=${EIB_SYSCONFDIR}/gnupg \
        --armour \
        --sign-with ${EIB_IMAGE_SIGNING_KEYID} \
        --detach-sign \
        --output "${outfile}" \
        "${file}"
  fi
}

# Emulate the old ostree write-refs builtin where a local ref is forced
# to the commit of another ref.
ostree_write_refs() {
  local repo=${1:?No ostree repo supplied to ${FUNCNAME}}
  local src=${2:?No ostree source ref supplied to ${FUNCNAME}}
  local dest=${3:?No ostree dest ref supplied to ${FUNCNAME}}
  local destdir=${dest%/*}

  # Create the needed directory for the dest ref.
  mkdir -p "${repo}/refs/heads/${destdir}"

  # Copy the source ref file to the dest ref.
  cp -f "${repo}/refs/heads/${src}" "${repo}/refs/heads/${dest}"
}

# Compress an image according to the configured compression type.
eib_compress_image() {
  case "${EIB_IMAGE_COMPRESSION}" in
    xz)
      # Memory is limited to 1GB so that we don't ENOMEM on 32 bit
      # builds and so the number of threads is limited on hosts with
      # lots of CPUs. This should still allow 12 threads when enough
      # memory and CPUs are available.
      xz -vv -M1G -T0 -4 -c "${1}" > "${2}"
      ;;
    gz)
      pigz --no-name -c "${1}" > "${2}"
      ;;
    *)
      echo "Unrecognized image compression ${EIB_IMAGE_COMPRESSION}" >&2
      return 1
      ;;
  esac
}

true
