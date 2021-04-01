#!/bin/bash
# -*- mode: Shell-script; sh-basic-offset: 2; indent-tabs-mode: nil -*-
shopt -s nullglob
shopt -s extglob

# Make sure ERR traps follow through to shell functions
set -E

# Show current script and time when xtrace (set -x) enabled. Will look
# like "+ 10:13:40 run-build: some command".
export PS4='+ \t ${BASH_SOURCE[0]##*/}: '

# Runs a command chrooted with our helper script.
chroot () {
  "${EIB_HELPERSDIR}"/eib-chroot "$@"
}

# Run hooks under customization/
run_hooks() {
  local hook interpreter
  local hooksdir="${EIB_HOOKSDIR:-${EIB_SRCDIR}/hooks}"
  local -a hooksdirs
  local group=$1
  local install_root=$2

  echo "Running $group hooks"
  export EIB_HOOK_GROUP=$group

  # Sort enabled hooks
  eval local hooks="\${EIB_${group^^}_HOOKS}"
  local files=$(echo "${hooks}" | tr ' ' '\n' | sort)

  # If a local settings directory is provided, look there first
  if [ -n "$EIB_LOCALDIR" ]; then
    hooksdirs+=("${EIB_LOCALDIR}/hooks")
  fi
  hooksdirs+=("${hooksdir}")

  for hook in ${files}; do
    local d
    local found
    local hookpath

    found=false
    for d in "${hooksdirs[@]}"; do
      hookpath="${d}/${group}/${hook}"
      if [ -f "${hookpath}" ]; then
        found=true
        break
      fi
    done

    if ! "${found}"; then
      echo "Missing hook ${hook} not found in ${hooksdirs[*]}" >&2
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

# Work around transient failures. The EIB_RETRY_ATTEMPTS and
# EIB_RETRY_INTERVAL environment variables can be used to change the
# defaults of 10 attempts with 1 second sleeps between attempts.
eib_retry() {
  local i=0
  local max_retries=${EIB_RETRY_ATTEMPTS:-10}
  local interval=${EIB_RETRY_INTERVAL:-1}

  if [ $# -eq 0 ]; then
    echo "error: No command supplied to ${FUNCNAME}" >&2
    return 1
  fi

  while ! "$@" && (( ++i < max_retries )) ; do
    echo "$@ failed; retrying..." >&2
    sleep "$interval"
  done

  if (( i >= max_retries )); then
    echo "$@ failed ${max_retries} times; giving up" >&2
    return 1
  fi
}

# Run udevadm settle to wait for device events to be processed. Tell
# udevadm to ignore that we're in a chroot since we expect the udev
# control socket to be bind mounted into it.
eib_udevadm_settle() {
  # If settle can't connect to the /run/udev/control socket, it will
  # simply return without an error. Print an error in that case but
  # carry on since skipping settle may not be fatal.
  if [ ! -e /run/udev/control ]; then
    echo '/run/udev/control does not exist when calling "udevadm settle"' >&2
    return 0
  fi

  # If the host udev version doesn't match the one in the build root, it
  # may fail so retry. In particular, this works around a race when
  # the host udev is older than version 242 but the build version is
  # newer. In that case, the host udevd closes the connection to the
  # control socket immediately after receiving the ping command.
  # However, newer udev sends a subsequent end of messages command and
  # may receive an EPIPE if that hasn't completed before udevd closes
  # the connection.
  #
  # Ultimately any errors are ignored in the hope that any device events
  # have been processed anyways. This is no different than when udevadm
  # settle was a no-op in the build root.
  #
  # https://phabricator.endlessm.com/T30938
  SYSTEMD_IGNORE_CHROOT=1 eib_retry udevadm settle || return 0
}

# Helpers for partx and losetup to work around races with device
# activity that cause the commands to fail with EBUSY.
eib_partx_scan() {
  eib_udevadm_settle
  eib_retry partx -a -v "$1"
}

eib_partx_delete() {
  eib_udevadm_settle
  eib_retry partx -d -v "$1"
}

eib_delete_loop() {
  eib_udevadm_settle
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

# Create a detached checksum with sha256sum.
checksum_file() {
  local file=${1:?No file supplied to ${FUNCNAME}}
  local outfile=${2:-${file}.sha256}

  sha256sum "${file}" | cut -d' ' -f1 > "${outfile}"
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
