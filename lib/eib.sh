#!/bin/bash
# -*- mode: Shell-script; sh-basic-offset: 2; indent-tabs-mode: nil -*-
shopt -s nullglob
shopt -s extglob

# Make sure ERR traps follow through to shell functions
set -E

# Show current script and time when xtrace (set -x) enabled. Will look
# like "+ run-build 10:13:40: some command".
export PS4='+ ${BASH_SOURCE[0]##*/} \t: '

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
      cp ${hookpath} $install_root/tmp/hook
      chroot $install_root $interpreter /tmp/hook
      rm -f $install_root/tmp/hook
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

  mount "$@"

  # The target is the last argument
  eval target="\${$#}"
  EIB_MOUNTS+=("${target}")
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

# Provide the path to the keyring file. If it doesn't exist, create it.
eib_keyring() {
  local keyring="${EIB_TMPDIR}"/eib-keyring.gpg
  local keysdir="${EIB_DATADIR}"/keys
  local -a keys
  local keyshome key

  # Create the keyring if necessary
  if [ ! -f "${keyring}" ]; then
    # Check that there are keys
    if [ ! -d "${keysdir}" ]; then
      echo "No gpg keys directory at ${keysdir}" >&2
      return 1
    fi
    keys=("${keysdir}"/*.asc)
    if [ ${#keys[@]} -eq 0 ]; then
      echo "No gpg keys in ${keysdir}" >&2
      return 1
    fi

    # Create a homedir with proper 0700 perms so gpg doesn't complain
    keyshome=$(mktemp -d --tmpdir="${EIB_TMPDIR}" eib-keyring.XXXXXXXXXX)

    # Import the keys
    for key in "${keys[@]}"; do
      gpg --batch --quiet --homedir "${keyshome}" --keyring "${keyring}" \
        --no-default-keyring --import "${key}"
    done

    # Set normal permissions for the keyring since gpg creates it 0600
    chmod 0644 "${keyring}"

    rm -rf "${keyshome}"
  fi

  echo "${keyring}"
}

eib_fix_boot_checksum() {
  local disk=${1:?No disk supplied to ${FUNCNAME}}
  local deploy=${2:?No deployment supplied to ${FUNCNAME}}

  [ -x "${deploy}"/usr/sbin/amlogic-fix-spl-checksum ] || return 0
  "${deploy}"/usr/sbin/amlogic-fix-spl-checksum "${disk}"
}

# Create a .inprogress file on the remote image server to indicate that
# this build has started publishing files.
eib_start_publishing() {
  local destdir="${EIB_IMAGE_DESTDIR}"

  # Skip on dry runs
  [ "${EIB_DRY_RUN}" = true ] && return 0

  if [ "$(hostname -s)" != "${EIB_IMAGE_HOST_SHORT}" ]; then
    ssh ${EIB_SSH_OPTIONS} ${EIB_IMAGE_USER}@${EIB_IMAGE_HOST} \
      mkdir -p "${destdir}"
    ssh ${EIB_SSH_OPTIONS} ${EIB_IMAGE_USER}@${EIB_IMAGE_HOST} \
      touch "${destdir}"/.inprogress
  else
    sudo -u ${EIB_IMAGE_USER} mkdir -p "${destdir}"
    sudo -u ${EIB_IMAGE_USER} touch "${destdir}"/.inprogress
  fi
}

# Delete the .inprogress file on the remote image server to indicate
# that this build has finished publishing files.
eib_end_publishing() {
  local destdir="${EIB_IMAGE_DESTDIR}"

  # Skip on dry runs
  [ "${EIB_DRY_RUN}" = true ] && return 0

  if [ "$(hostname -s)" != "${EIB_IMAGE_HOST_SHORT}" ]; then
    ssh ${EIB_SSH_OPTIONS} ${EIB_IMAGE_USER}@${EIB_IMAGE_HOST} \
      rm -f "${destdir}"/.inprogress
  else
    sudo -u ${EIB_IMAGE_USER} rm -f "${destdir}"/.inprogress
  fi
}

# Delete the in progess image publishing directory on failure.
eib_fail_publishing() {
  local destdir="${EIB_IMAGE_DESTDIR}"

  # Skip on dry runs
  [ "${EIB_DRY_RUN}" = true ] && return 0

  # If the .inprogress file exists, delete the entire destdir. This is
  # pretty ugly because we need a shell command list and that would
  # require quite a bit of magic escaping.
  if [ "$(hostname -s)" != "${EIB_IMAGE_HOST_SHORT}" ]; then
    if ssh ${EIB_SSH_OPTIONS} ${EIB_IMAGE_USER}@${EIB_IMAGE_HOST} \
      test -f "${destdir}"/.inprogress
    then
      ssh ${EIB_SSH_OPTIONS} ${EIB_IMAGE_USER}@${EIB_IMAGE_HOST} \
        rm -rf "${destdir}"
    fi
  else
    if sudo -u ${EIB_IMAGE_USER} test -f "${destdir}"/.inprogress; then
      sudo -u ${EIB_IMAGE_USER} rm -rf "${destdir}"
    fi
  fi
}

# Try to work around a race where partx sometimes reports EBUSY failure
eib_partx_scan() {
  udevadm settle
  local i=0
  while ! partx -a -v "$1"; do
	(( ++i ))
	[ $i -ge 10 ] && break
    echo "partx scan $1 failed, retrying..."
    sleep 1
  done
}

# Work around a race where loop deletion sometimes fails with EBUSY
eib_delete_loop() {
  udevadm settle
  local i=0
  while ! losetup -d "$1"; do
	(( ++i ))
	[ $i -ge 10 ] && break
    echo "losetup remove $1 failed, retrying..."
    sleep 1
  done
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

jenkins_crumb() {
  local token=$(<"${EIB_JENKINS_TOKEN}")

  curl -u "${EIB_JENKINS_USER}:${token}" \
    "${EIB_JENKINS_URL}"'/crumbIssuer/api/xml?xpath=concat(//crumbRequestField,":",//crumb)'
}

# Compress an image according to the configured compression type.
eib_compress_image() {
  case "${EIB_IMAGE_COMPRESSION}" in
    xz)
      xz -T0 -c "${1}" > "${2}"
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
