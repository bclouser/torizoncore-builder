"""Common OSTree functions

Helper functions for commonly used OSTree functions.
"""

import logging
import os
import re
import subprocess
import traceback
import threading

from functools import partial
from http.server import SimpleHTTPRequestHandler, HTTPServer

from tcbuilder.errors import TorizonCoreBuilderError, PathNotExistError

# pylint: disable=wrong-import-order,wrong-import-position
import gi
gi.require_version("OSTree", "1.0")
from gi.repository import Gio, GLib, OSTree
# pylint: enable=wrong-import-order,wrong-import-position

log = logging.getLogger("torizon." + __name__)

OSTREE_BASE_REF = "base"
DEFAULT_SERVER_PORT = 8080

# Whiteout defines match what Containers are using:
# https://github.com/opencontainers/image-spec/blob/v1.0.1/layer.md#whiteouts
# this is from src/libostree/ostree-repo-checkout.c
OSTREE_WHITEOUT_PREFIX = ".wh."
OSTREE_OPAQUE_WHITEOUT_NAME = ".wh..wh..opq"

def open_ostree(ostree_dir):
    repo = OSTree.Repo.new(Gio.File.new_for_path(ostree_dir))
    if not repo.open(None):
        raise TorizonCoreBuilderError("Opening the archive OSTree repository failed.")
    return repo

def create_ostree(ostree_dir, mode: OSTree.RepoMode = OSTree.RepoMode.ARCHIVE_Z2):
    repo = OSTree.Repo.new(Gio.File.new_for_path(ostree_dir))
    repo.create(mode, None)
    return repo

def load_sysroot(sysroot_dir):
    sysroot = OSTree.Sysroot.new(Gio.File.new_for_path(sysroot_dir))
    sysroot.load()
    return sysroot

def get_deployment_info_from_sysroot(sysroot):
    # Get commit csum and kernel arguments from the currenty sysroot

    # There is a single deployment in our OSTree sysroots
    deployment = sysroot.get_deployments()[0]

    # Get the origin refspec
    #refhash = deployment.get_origin().get_string("origin", "refspec")

    bootparser = deployment.get_bootconfig()
    kargs = bootparser.get('options')
    csum = deployment.get_csum()
    sysroot.unload()

    return csum, kargs


def get_metadata_from_checksum(repo, csum):
    result, commitvar, _state = repo.load_commit(csum)
    if not result:
        raise TorizonCoreBuilderError(f"Error loading commit {csum}.")

    # commitvar is GLib.Variant, use unpack to get a Python dictionary
    commit = commitvar.unpack()

    # Unpack commit object, see OSTree src/libostree/ostree-repo-commit.c
    metadata, _parent, _, subject, body, _time, _content_csum, _metadata_csum = commit

    return metadata, subject, body

def get_metadata_from_ref(repo, ref):
    result, _, csum = repo.read_commit(ref)
    if not result:
        raise TorizonCoreBuilderError(f"Error loading commit {ref}.")

    return get_metadata_from_checksum(repo, csum)


def pull_remote(repo, name, remote, refs, token, progress=None):
    """
    Function to pull OStree from remote.

    :param repo: Static delta repo
    :param name: Remote name
    :param remote: Remote url to full from
    :param refs: Commits to pull
    :param token: Access token
    :param progress: Async progress handler
    """

    options = GLib.Variant("a{sv}", {
        "gpg-verify": GLib.Variant("b", False)
    })

    if not repo.remote_add(name, remote, options=options):
        raise TorizonCoreBuilderError(f"Error adding remote {remote}.")

    options = GLib.Variant("a{sv}", {
        "flags": GLib.Variant("i", OSTree.RepoPullFlags.MIRROR & OSTree.RepoPullFlags.TRUSTED_HTTP),
        "http-headers": GLib.Variant("a(ss)", [("Authorization", f"Bearer {token}")]),
        "refs": GLib.Variant.new_strv(refs)
    })

    if progress is not None:
        asyncprogress = OSTree.AsyncProgress.new()
        asyncprogress.connect("changed", progress)
    else:
        asyncprogress = None

    log.info("Pulling commits...")
    if not repo.pull_with_options(name, options, progress=asyncprogress):
        raise TorizonCoreBuilderError("Error pulling contents from remote repository.")

    if asyncprogress is not None:
        asyncprogress.set_status("Pull completed")
        asyncprogress.finish()


def generate_delta(repo, from_delta, to_delta):
    """
    Function to generate static delta.

    :param repo: Static delta repo.
    :param from_delta: The OSTree commit to create a static delta from
    :param to_delta: The OSTree commit to create a static delta to
    """

    result = repo.static_delta_generate(OSTree.StaticDeltaGenerateOpt.MAJOR,
                                        from_delta,
                                        to_delta,
                                        None,
                                        GLib.Variant("a{sv}", None),
                                        None)

    if not result:
        raise TorizonCoreBuilderError("Error generating static delta.")


def pull_remote_ref(repo, uri, ref, remote=None, progress=None):
    options = GLib.Variant("a{sv}", {
        "gpg-verify": GLib.Variant("b", False)
    })

    log.debug(f"Pulling remote {uri} reference {ref}")

    if not repo.remote_add("origin", remote, options=options):
        raise TorizonCoreBuilderError(f"Error adding remote {remote}.")

    # ostree --repo=toradex-os-tree pull origin torizon/torizon-core-docker --depth=0

    options = GLib.Variant("a{sv}", {
        "refs": GLib.Variant.new_strv([ref]),
        "depth": GLib.Variant("i", 0),
        "override-remote-name": GLib.Variant('s', remote),
    })

    if progress is not None:
        asyncprogress = OSTree.AsyncProgress.new()
        asyncprogress.connect("changed", progress)
    else:
        asyncprogress = None

    if not repo.pull_with_options("origin", options, progress=asyncprogress):
        raise TorizonCoreBuilderError("Error pulling contents from local repository.")


def get_reference_dict(repopath, base_csum=None):
    """
    Get all the references in a ostree repo, excluding the OSTree generated ref.

    :param repopath: Absolute path of local repository to pull from.
    :param base_csum: Checksum that will be assinged to the 'base' ref.
    :returns:
        A dict with the reference as key and the checksum as value.
        e.g: {'base': <checksum>}
    """
    ref_dict = {} if base_csum is None else {OSTREE_BASE_REF: base_csum}
    sysroot_repo = open_ostree(repopath)
    for ref_name, ref_csum in sysroot_repo.list_refs().out_all_refs.items():
        # Filter out the remote and the OSTree generated ref
        if re.match(r"ostree/[0-9]+/[0-9]+/[0-9]+", ref_name) is None:
            ref_name = ref_name.split(":", 1)[-1].lstrip()
            ref_dict[ref_name] = ref_csum

    return ref_dict


def pull_local_refs(repo: OSTree.Repo, repopath: str, refs: str, remote=None):
    """
    Fetches references from local repository.

    :param repo: OSTree.Repo object.
    :param repopath: Absolute path of local repository to pull from.
    :param refs: Remote reference to pull.
    :param remote: Remote name used in refspec.
    """
    # With Bullseye's ostree version 2020.7, the following snippet fails with:
    # gi.repository.GLib.GError: g-io-error-quark: Remote "torizon" not found (1)
    #
    #    options = GLib.Variant("a{sv}", {
    #        "refs": GLib.Variant.new_strv([csum]),
    #        "depth": GLib.Variant("i", 0),
    #        "override-remote-name": GLib.Variant('s', remote),
    #    })
    #    if not repo.pull_with_options("file://" + repopath, options):
    #        raise TorizonCoreBuilderError(
    #            f"Error pulling contents from local repository {repopath}.")
    #
    # Work around by employing the ostree CLI instead.
    repo_fd = repo.get_dfd()
    repo_str = os.readlink(f"/proc/self/fd/{repo_fd}")

    try:
        for ref_name, ref_csum in refs.items():
            log.debug(f"Pulling from local repository {repopath} commit checksum {ref_csum}")
            subprocess.run(
                [arg for arg in [
                    "ostree",
                    "pull-local",
                    f"--repo={repo_str}",
                    f"--remote={remote}" if remote else None,
                    repopath,
                    ref_csum] if arg],
                check=True)
            repo.reload_config()
            # Note: In theory we can do this with two options in one go, but that seems
            # to validate ref-bindings... (has probably something to do with Collection IDs etc..)
            #"refs": GLib.Variant.new_strv(["base"]),
            #"override-commit-ids": GLib.Variant.new_strv([ref]),
            repo.set_collection_ref_immediate(OSTree.CollectionRef.new(None, ref_name), ref_csum)
    except subprocess.CalledProcessError as exc:
        logging.error(traceback.format_exc())
        raise TorizonCoreBuilderError(
            f"Error pulling contents from local repository {repopath}.") from exc



def _convert_gio_file_type(gio_file_type):
    res = None
    if gio_file_type == Gio.FileType.DIRECTORY:
        res = 'directory'
    elif gio_file_type == Gio.FileType.MOUNTABLE:
        res = 'mountable'
    elif gio_file_type == Gio.FileType.REGULAR:
        res = 'regular'
    elif gio_file_type == Gio.FileType.SHORTCUT:
        res = 'shortcut'
    elif gio_file_type == Gio.FileType.SPECIAL:
        res = 'special'
    elif gio_file_type == Gio.FileType.SYMBOLIC_LINK:
        res = 'symbolic_link'
    elif gio_file_type == Gio.FileType.UNKNOWN:
        res = 'unknown'
    else:
        raise TorizonCoreBuilderError(f"Unknown gio filetype {gio_file_type}")
    return res


def check_existance(repo, commit, path):
    path = os.path.realpath(path)

    ret, root, _commit = repo.read_commit(commit)
    if not ret:
        raise TorizonCoreBuilderError(f"Error couldn't reat commit: {commit}")

    sub_path = root.resolve_relative_path(path)
    return sub_path.query_exists()


# pylint: disable=invalid-name
def ls(repo, path, commit):
    """ return a list of files and directories in a ostree repo under path

        args:
            repo(OSTree.Repo) - repo object
            path(str) - absolute path which we want to enumerate
            commit(str) - the ostree commit hash or name

        return:
            file_list(list) - list of files and directories under path

        raises:
            TorizonCoreBuilderError - if commit does not exist
            PathNotExistError - if path does not exist
    """
    # Make sure we don't end the path with / because this confuses ostree
    path = os.path.realpath(path)

    ret, root, _commit = repo.read_commit(commit)
    if not ret:
        raise TorizonCoreBuilderError(f"Error couldn't reat commit: {commit}")

    sub_path = root.resolve_relative_path(path)
    if sub_path.query_exists():
        file_list = sub_path.enumerate_children(
            "*", Gio.FileQueryInfoFlags.NOFOLLOW_SYMLINKS, None)

        return list(map(lambda f: {
            "name": f.get_name(),
            "type": _convert_gio_file_type(f.get_file_type())
        }, file_list))

    raise PathNotExistError(f"path {path} does not exist")
# pylint: enable=invalid-name


def get_kernel_version(repo, commit):
    """ return the kernel version used in the commit

        args:
            repo(OSTree.Repo) - repo object
            commit(str) - the ostree commit hash or name

        return:
            version(str) - The kernel version used in this OSTree commit
    """

    kernel_version = ""

    module_files = ls(repo, "/usr/lib/modules", commit)
    module_dirs = filter(lambda file: file["type"] == "directory",
                         module_files)

    # This is a similar approach to what OSTree does in the deploy command.
    # It searches for the directory under /usr/lib/modules/<kver> which
    # contains a vmlinuz file.
    for module_dir in module_dirs:
        directory_name = module_dir["name"]

        # Check if the directory contains a vmlinuz image if so it is our
        # kernel directory
        files = ls(repo, f"/usr/lib/modules/{directory_name}", commit)
        if any(file for file in files if file["name"] == "vmlinuz"):
            kernel_version = directory_name
            break

    return kernel_version

def copy_file(repo, commit, input_file, output_file):
    """ copy a file within a OSTree repo to somewhere else

        args:
            repo(OSTree.Repo) - repo object
            commit(str) - the ostree commit hash or name
            input_file - the input file path in the OSTree
            output_file - the output file paht where we want to copy to
        raises:
            TorizonCoreBuilderError - if commit does not exist
    """

    # Make sure we don't end the path with / because this confuses ostree
    ret, root, _commit = repo.read_commit(commit)
    if not ret:
        raise TorizonCoreBuilderError(f"Can not read commit: {commit}")

    input_stream = root.resolve_relative_path(input_file).read()

    output_stream = Gio.File.new_for_path(output_file).create(
        Gio.FileCreateFlags.NONE, None)
    if not output_stream:
        raise TorizonCoreBuilderError(f"Can not create file {output_file}")

    # Move input to output stream
    output_stream.splice(
        input_stream, Gio.OutputStreamSpliceFlags.CLOSE_SOURCE, None)


class TCBuilderHTTPRequestHandler(SimpleHTTPRequestHandler):
    """SimpleHTTPRequestHandler which makes use of logging framework"""

    def __init__(self, *args, **kwargs):
        self.log = logging.getLogger("torizon." + __name__)
        super().__init__(*args, **kwargs)

    #pylint: disable=redefined-builtin,logging-not-lazy
    def log_message(self, format, *args):
        self.log.debug(format % args)


class HTTPThread(threading.Thread):
    """HTTP Server thread"""

    def __init__(self, directory, host="", port=DEFAULT_SERVER_PORT):
        threading.Thread.__init__(self, daemon=True)

        self.log = logging.getLogger("torizon." + __name__)
        self.log.info("Starting http server to serve OSTree.")

        # From what I understand, this creates a __init__ function with the
        # directory argument already set. Nice hack!
        handler_init = partial(TCBuilderHTTPRequestHandler, directory=directory)
        self.http_server = HTTPServer((host, port), handler_init)

    def run(self):
        self.http_server.serve_forever()

    def shutdown(self):
        """Shutdown HTTP server"""
        self.log.debug("Shutting down http server.")
        self.http_server.shutdown()

    @property
    def server_port(self):
        return self.http_server.server_port

    @property
    def server_address(self):
        return self.http_server.server_address


def serve_ostree_start(ostree_dir, host="", port=DEFAULT_SERVER_PORT):
    """Serving given path via http"""
    http_thread = HTTPThread(ostree_dir, host, port)
    http_thread.start()
    return http_thread


def serve_ostree_stop(http_thread):
    """Stop serving"""
    http_thread.shutdown()
