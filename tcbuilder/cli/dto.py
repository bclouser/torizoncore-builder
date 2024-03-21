"""CLI handling for dto subcommand."""

import logging
import os
import shlex
import shutil
import subprocess
import sys
import tempfile

from tcbuilder.backend import dt, dto, common
from tcbuilder.backend.common import images_unpack_executed, unpacked_image_type

from tcbuilder.cli import images as images_cli
from tcbuilder.cli import dt as dt_cli
from tcbuilder.cli import union as union_cli
from tcbuilder.cli import deploy as deploy_cli

from tcbuilder.errors import InvalidDataError

log = logging.getLogger("torizon." + __name__)

# Dear maintainer, the following code employs these abbreviations as pieces of variable names:
# - dts: device tree source
# - dtb: device tree blob
# - dtos: device tree overlay source
# - dtob device tree overlay blob
# - path: full path to a file
# - dir: full path to a directory
# - basename: name of a file (no directory information)
# - tmp: temporary resource in filesystem
# - target: functional output artifact in filesystem


# pylint: disable=too-many-locals
def dto_apply(dtos_path, dtb_path, include_dirs, storage_dir,
              allow_reapply=False, test_apply=True):
    '''Execute most of the work of 'dto apply' command.

    :param dtos_path: the full path to the source device-tree overlay file to be applied.
    :param dtb_path: the full path to the blob file where to test apply the overlay (required
                     only if `test_apply` is True).
    :param include_dirs: list of directories where to search include files when building the
                         overlay file.
    :param storage_dir: path to root directory where most operations will be performed.
    :param allow_reapply: whether or not to allow an overlay to be applied another time.
    :param test_apply: whether or not to apply the overlay over the device tree to check for
                       errors.
    '''

    images_unpack_executed(storage_dir)
    if unpacked_image_type(storage_dir) == "wic":
        raise InvalidDataError("dto commands are not supported for WIC images. "
                               "Aborting.")

    applied_overlay_basenames = dto.get_applied_overlays_base_names(storage_dir)
    dtob_target_basename = os.path.splitext(os.path.basename(dtos_path))[0] + ".dtbo"

    # Detect a redundant overlay application.
    if not allow_reapply:
        if dtob_target_basename in applied_overlay_basenames:
            log.error(f"error: overlay {dtob_target_basename} is already applied.")
            sys.exit(1)

    # In case the user is reapplying an overlay we remove it from the current list
    # to ensure only the last application will take effect.
    if dtob_target_basename in applied_overlay_basenames:
        applied_overlay_basenames.remove(dtob_target_basename)

    # Compile the overlay.
    with tempfile.NamedTemporaryFile(delete=False) as tmpf:
        dtob_tmp_path = tmpf.name
    if not dt.build_dts(dtos_path, include_dirs, dtob_tmp_path):
        log.error(f"error: cannot apply {dtos_path}.")
        sys.exit(1)

    # Test apply the overlay against the current device tree and other applied overlays.
    if test_apply:
        if dtb_path:
            # User has provided the basename of a device tree blob of the base image.
            (any_dtb_path, _) = dt.get_current_dtb_path(storage_dir)
            dtb_path = os.path.join(os.path.dirname(any_dtb_path), dtb_path)
        else:
            # Use the current device tree blob.
            (dtb_path, is_dtb_exact) = dt.get_current_dtb_path(storage_dir)
            if not is_dtb_exact:
                log.error("error: could not find the device tree to check the overlay against.")
                log.error("Please use --device-tree to pass one of the device trees below or use "
                          "--force to bypass checking:")
                dtb_list = subprocess.check_output(
                    ["find", os.path.dirname(dtb_path), "-maxdepth", "1", "-type", "f",
                     "-name", "*.dtb", "-printf", "- %f\\n"],
                    text=True).rstrip()
                log.error(dtb_list)
                sys.exit(1)

        applied_overlay_paths = \
            dto.get_applied_overlay_paths(storage_dir, base_names=applied_overlay_basenames)
        with tempfile.NamedTemporaryFile(delete=False) as tmpf:
            dtb_tmp_path = tmpf.name
        if not dto.modify_dtb_by_overlays(dtb_path,
                                          applied_overlay_paths + [dtob_tmp_path], dtb_tmp_path):
            log.error(f"error: overlay '{dtos_path}' is not applicable.")
            sys.exit(1)
        log.info(f"'{dtob_target_basename}' can successfully modify the device "
                 f"tree '{os.path.basename(dtb_path)}'.")

    # Deploy the device tree overlay blob.
    dt_changes_dir = dt.get_dt_changes_dir(storage_dir)
    dtob_target_dir = os.path.join(
        dt_changes_dir, dt.get_dtb_kernel_subdir(storage_dir), "overlays")
    os.makedirs(dtob_target_dir, exist_ok=True)
    dtob_target_path = os.path.join(dtob_target_dir, dtob_target_basename)
    shutil.move(dtob_tmp_path, dtob_target_path)

    # Deploy the enablement of the device tree overlay blob.
    new_overlay_basenames = applied_overlay_basenames + [dtob_target_basename]
    overlays_txt_target_path = \
        os.path.join(dt_changes_dir, dt.get_dtb_kernel_subdir(storage_dir), "overlays.txt")
    with open(overlays_txt_target_path, "w") as ovlf:
        ovlf.write("fdt_overlays=" + " ".join(new_overlay_basenames) + "\n")

    # All set :-)
    log.info(f"Overlay {dtob_target_basename} successfully applied.")

# pylint: enable=too-many-locals


def do_dto_apply(args):
    '''Perform the 'dto apply' command.'''

    # Sanity check parameters.
    if not args.include_dirs:
        args.include_dirs = ["device-trees/include"]
    assert args.dtos_path, "panic: missing overlay source parameter"

    if args.force:
        log.info("warning: --force was used, bypassing checking overlays against the device tree.")

    dto_apply(dtos_path=args.dtos_path,
              dtb_path=args.device_tree,
              include_dirs=args.include_dirs,
              storage_dir=args.storage_directory,
              allow_reapply=False,
              test_apply=not args.force)


def do_dto_list(args):
    '''Perform the 'dto list' command.'''

    # Sanity check for overlay sources to scan.
    overlays_subdir = "device-trees/overlays"
    if not os.path.isdir(overlays_subdir):
        log.error("error: missing device tree overlays directory "
                  f"'{overlays_subdir}' -- see dt checkout")
        sys.exit(1)

    # Sanity check for --device-tree
    if args.device_tree and not (args.device_tree.endswith(".dtb") or
                                 args.device_tree.endswith(".dts")):
        log.error("Please pass either a device tree source file or device "
                  "tree binary to --device-tree.")
        sys.exit(1)

    images_unpack_executed(args.storage_directory)
    if unpacked_image_type(args.storage_directory) == "wic":
        raise InvalidDataError("dto commands are not supported for WIC images. "
                               "Aborting.")

    # Find a device tree to check overlay compatibility against.
    dtb_path = args.device_tree
    if dtb_path and not os.path.isfile(dtb_path) and dtb_path.endswith(".dts"):
        # The user has passed a wrong device tree blob file with --device-tree
        log.error(f"error: cannot read device tree source '{dtb_path}'.")
        sys.exit(1)
    is_dtb_exact = True
    if not dtb_path or dtb_path.endswith(".dtb"):
        # The user has not issued --device-tree; take the applied device tree instead.
        (dtb_path, is_dtb_exact) = dt.get_current_dtb_path(args.storage_directory)
    if not is_dtb_exact and not args.device_tree:
        log.error("Could not determine default device tree.")
        dtb_list = subprocess.check_output(
            ["find", os.path.dirname(dtb_path), "-maxdepth", "1", "-type", "f",
             "-name", "*.dtb", "-printf", "- %f\\n"],
            text=True).rstrip()
        if dtb_list.count('\n') > 0:
            log.error("Please use --device-tree to pass one of the device "
                      "trees below as the assumed default:")
            log.error(dtb_list)
            sys.exit(1)
        else:
            log.info("Proceeding with the following device tree as the assumed default:")
            log.info(dtb_list)
    if args.device_tree and args.device_tree.endswith(".dtb"):
        dtb_path = os.path.join(os.path.dirname(dtb_path), args.device_tree)

    # Extract compatibility labels from the device tree blob,
    # and use them for building regexp patterns for matching with compatible
    # device tree source files.
    #
    # Samples of such patterns:
    # ^[[:blank:]]*compatible *= *"toradex,colibri-imx8x-aster"
    # ^[[:blank:]]*compatible *= *"toradex,colibri-imx8x"
    # ^[[:blank:]]*compatible *= *"fsl,imx8qxp"
    with tempfile.NamedTemporaryFile(delete=False) as tmpf:
        compat_regexps_tmp_path = tmpf.name
    if args.device_tree and args.device_tree.endswith(".dts"):
        # The user passed a device tree source file to check compatibility against;
        # parse the textual content of the file.
        try:
            # About the 'sed' invocations below:
            # 1. The first 'sed' scans the device tree source file and extracts
            #    the first block from "compatible =" to the semicolon.
            # 2. The second sed filters out the "source noise" of the
            #    compatibility values;
            # 3. The final 'sed' prepends '^[[:blank:]]*compatible *= *'
            #    to the compatibility values.
            subprocess.check_output(
                "set -o pipefail && "
                "sed -r -e '/^[[:blank:]]*compatible *=/,/;/!d' "
                f"-e '/;/q' {shlex.quote(dtb_path)} | tr -d '\n' | "
                "sed -r -e 's/.*\\<compatible *= *//' "
                "-e 's/[[:blank:]]*//g' -e 's/\";.*/\"\\n/' -e 's/\",\"/\"\\n\"/g'"
                f">{shlex.quote(compat_regexps_tmp_path)}",
                shell=True, text=True, stderr=subprocess.STDOUT)
        except subprocess.CalledProcessError as exc:
            log.error(exc.output.strip())
            log.error("error: cannot extract compatibility labels from device "
                      f"tree source '{dtb_path}'")
            sys.exit(1)
    else:
        # The device tree is a blob file from the image.
        try:
            # About the 'sed' programs below:
            #  -e 's/$/\"/' appends '"' to each line
            subprocess.check_output(
                f"set -o pipefail && fdtget {shlex.quote(dtb_path)} / compatible | tr ' ' '\n' "
                f"| sed -e 's/$/\"/' >{shlex.quote(compat_regexps_tmp_path)}",
                shell=True, text=True, stderr=subprocess.STDOUT)
        except subprocess.CalledProcessError as exc:
            log.error(exc.output.strip())
            if "FDT_ERR_BADMAGIC" in exc.output:
                log.error(f"error: bad file format -- is '{dtb_path}' a device tree blob?")
            else:
                log.error("error: cannot extract compatibility labels from "
                          f"device tree blob '{dtb_path}'")
            sys.exit(1)

    # Show all device tree overlay source files that are compatible with the device tree blob.
    # Given the regexp patterns mentioned above, 'grep' can easily scan for all compatible
    # files under a given subdirectory.
    try:
        compat_list = subprocess.check_output(
            f"set -o pipefail && grep -rlHEf {shlex.quote(compat_regexps_tmp_path)} "
            f"{shlex.quote(overlays_subdir)} "
            "| sort -u | sed -e 's/^/- /'", shell=True, text=True).strip()
        log.info(f"Overlays compatible with device tree {os.path.basename(dtb_path)}:")
        log.info(f"{compat_list}")
    except: # pylint: disable=W0702
        log.info("No overlays compatible with device tree "
                 f"{os.path.basename(dtb_path)} were found.")


def do_dto_status(args):
    '''Perform the 'dto status' command.'''

    images_unpack_executed(args.storage_directory)
    if unpacked_image_type(args.storage_directory) == "wic":
        raise InvalidDataError("dto commands are not supported for WIC images. "
                               "Aborting.")

    # Show the enabled device tree.
    (dtb_path, is_dtb_exact) = dt.get_current_dtb_path(args.storage_directory)
    dtb_basename = os.path.basename(dtb_path)
    if is_dtb_exact:
        log.info(f"Enabled overlays over device tree {dtb_basename}:")
    else:
        log.info("Enabled overlays over unknown device tree:")

    # Show the enabled overlays.
    for overlay_basename in dto.get_applied_overlays_base_names(args.storage_directory):
        log.info(f"- {overlay_basename}")


def dto_remove_single(dtob_basename, storage_dir, presence_required=True):
    '''Remove a single overlay.'''

    images_unpack_executed(storage_dir)
    if unpacked_image_type(storage_dir) == "wic":
        raise InvalidDataError("dto commands are not supported for WIC images. "
                               "Aborting.")

    dtob_basenames = dto.get_applied_overlays_base_names(storage_dir)
    if not dtob_basename in dtob_basenames:
        if presence_required:
            log.error(f"error: overlay '{dtob_basename}' is already not applied.")
            sys.exit(1)
        else:
            log.debug(f"Overlay '{dtob_basename}' is already not applied.")
            return False

    log.debug(f"Removing overlay '{dtob_basename}'")
    dtob_basenames.remove(dtob_basename)

    # Create a overlays.txt file without the reference to the removed overlay.
    dt_changes_dir = dt.get_dt_changes_dir(storage_dir)
    overlays_txt_target_path = \
        os.path.join(dt_changes_dir, dt.get_dtb_kernel_subdir(storage_dir), "overlays.txt")
    os.makedirs(os.path.dirname(overlays_txt_target_path), exist_ok=True)
    with open(overlays_txt_target_path, "w") as ovlf:
        ovlf.write("fdt_overlays=" + " ".join(dtob_basenames) + "\n")

    # Remove the overlay blob if it's not deployed.
    dtob_path = dto.find_path_to_overlay(storage_dir, dtob_basename)
    if dtob_path.startswith(dt_changes_dir):
        os.remove(dtob_path)

    return True


def dto_remove_all(storage_dir):
    '''Remove all overlays currently applied.'''

    images_unpack_executed(storage_dir)
    if unpacked_image_type(storage_dir) == "wic":
        raise InvalidDataError("dto commands are not supported for WIC images. "
                               "Aborting.")

    log.debug("Removing all overlays")

    # Deploy an empty overlays config file.
    dt_changes_dir = dt.get_dt_changes_dir(storage_dir)
    overlays_txt_target_path = os.path.join(
        dt_changes_dir, dt.get_dtb_kernel_subdir(storage_dir), "overlays.txt")
    os.makedirs(os.path.dirname(overlays_txt_target_path), exist_ok=True)
    with open(overlays_txt_target_path, "w") as file:
        file.write("fdt_overlays=\n")

    # Wipe out all overlay blobs as external changes.
    dtob_target_dir = os.path.join(
        dt_changes_dir, dt.get_dtb_kernel_subdir(storage_dir), "overlays")
    shutil.rmtree(dtob_target_dir, ignore_errors=True)

    # Sanity check.
    assert not dto.get_applied_overlays_base_names(storage_dir), \
        "panic: all overlays removal failed; please contact the maintainers of this tool."


def do_dto_remove(args):
    '''Perform the 'dto status' command.'''

    if args.all and args.dtob_basename:
        log.error("error: both --all and an overlay were specified in the command line.")
        sys.exit(1)

    if args.all:
        # The user wants to remove all overlays.
        dto_remove_all(args.storage_directory)

    else:
        # The user wants to remove a single overlay.
        if not args.dtob_basename:
            log.error("error: no overlay was specified in the command line.")
            sys.exit(1)

        dto_remove_single(args.dtob_basename, args.storage_directory, presence_required=True)


def do_dto_deploy(args):
    """
    Run just one command to deploy an overlay in the device, so it is easier
    and less error-prone to the user.
    """

    # Download TEZI image and checkout Device Tree files.
    args.remove_storage = True
    setattr(args, 'update', False)
    images_cli.do_images_download(args)
    dt_cli.do_dt_checkout(args)

    # Remove all applied overlays
    if args.clear:
        args.all = True
        args.dtob_basename = None
        do_dto_remove(args)

    # Apply all Device Tree overlay file(s) passed in the command line.
    for dtos_path in args.dtos_paths:
        args.dtos_path = dtos_path
        do_dto_apply(args)

    # Create an ostree overlay.
    union_branch = "dto_deploy"
    union_cli.union(
        changes_dirs=None,
        storage_dir=args.storage_directory,
        union_branch=union_branch,
        commit_subject="dto_deploy_subject",
        commit_body="dto_deploy_body")

    # Deploy an ostree overlay in the device.
    args.ref = union_branch
    deploy_cli.do_deploy_ostree_remote(args)


def init_parser(subparsers):
    '''Initializes the 'dto' subcommands command line interface.'''

    parser = subparsers.add_parser(
        "dto",
        description="Manage device tree overlays",
        help="Manage device tree overlays",
        allow_abbrev=False)

    subparsers = parser.add_subparsers(title='Commands', required=True, dest='cmd')

    # dto apply
    subparser = subparsers.add_parser(
        "apply",
        description="Apply a device tree overlay",
        help="Apply a device tree overlay",
        allow_abbrev=False)
    subparser.add_argument(
        metavar="OVERLAY", dest="dtos_path",
        help="Path to the device tree overlay source file")
    subparser.add_argument(
        "--include-dir",
        metavar="DIR", dest="include_dirs", action='append',
        help=("Search directory for include files during overlay compilation. "
              "Can be passed multiple times. If absent, defaults to 'device-trees/include'"))
    subparser.add_argument(
        "--device-tree",
        metavar="FILE", dest="device_tree",
        help="Test the overlay against an specific device tree.")
    subparser.add_argument(
        "--force",
        action="store_true",
        help="Apply the overlay even on failure checking it against a device tree.")
    subparser.set_defaults(func=do_dto_apply)

    # dto list
    subparser = subparsers.add_parser(
        "list",
        description="List the device tree overlays compatible with the current device tree",
        help="List the device tree overlays compatible with the current device tree",
        allow_abbrev=False)
    subparser.add_argument(
        "--device-tree",
        metavar="FILE", dest="device_tree",
        help=("Check for overlay compatibility against this device tree source or binary "
              "file instead."))
    subparser.set_defaults(func=do_dto_list)

    # dto status
    subparser = subparsers.add_parser(
        "status",
        description="List the applied device tree overlays",
        help="List the applied device tree overlays",
        allow_abbrev=False)
    subparser.set_defaults(func=do_dto_status)

    # dto remove
    subparser = subparsers.add_parser(
        "remove",
        description="Remove a device tree overlay",
        help="Remove a device tree overlay",
        allow_abbrev=False)
    subparser.add_argument(
        metavar="OVERLAY", dest="dtob_basename", nargs='?',
        help="Name of the device tree overlay")
    subparser.add_argument(
        "--all",
        action="store_true",
        help="Remove all device tree overlays")
    subparser.set_defaults(func=do_dto_remove)

    # dto deploy
    subparser = subparsers.add_parser(
        "deploy",
        description="Deploy a device tree overlay in the device",
        help="Deploy a device tree overlay in the device",
        allow_abbrev=False)
    subparser.add_argument(
        "--remote-host",
        dest="remote_host",
        help="Name/IP of remote machine", required=True)
    common.add_ssh_arguments(subparser)
    subparser.add_argument(
        "--reboot",
        dest="reboot", action='store_true',
        help="Reboot device after deploying device tree overlay(s)",
        default=False)
    subparser.add_argument(
        "--mdns-source",
        dest="mdns_source",
        help=("Use the given IP address as mDNS source. This is useful when "
              "multiple interfaces are used, and mDNS multicast requests are "
              "sent out the wrong network interface."))
    subparser.add_argument(
        "--include-dir",
        metavar="DIR", dest="include_dirs", action='append',
        help=("Search directory for include files during overlay compilation. "
              "Can be passed multiple times. If absent, defaults to "
              "'device-trees/include'"))
    subparser.add_argument(
        "--force",
        action="store_true",
        help="Apply the overlay even on failure checking it against a device tree.")
    subparser.add_argument(
        "--device-tree",
        metavar="FILE", dest="device_tree",
        help="Test the overlay against an specific device tree.")
    subparser.add_argument(
        "--clear",
        dest="clear", action="store_true",
        help="Remove all currently applied device tree overlays.", default=False)
    subparser.add_argument(
        metavar="OVERLAY", dest="dtos_paths",
        help="Path to the device tree overlay source file(s)", nargs='+')
    subparser.set_defaults(func=do_dto_deploy)
