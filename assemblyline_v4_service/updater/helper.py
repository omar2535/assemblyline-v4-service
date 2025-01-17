import certifi
import os
import regex as re
import requests
import shutil
import tempfile
import time

from git import Repo
from typing import List, Dict, Any, Tuple
from urllib.parse import urlparse
from shutil import make_archive

from assemblyline.common.isotime import iso_to_epoch
from assemblyline.common.digests import get_sha256_for_file


BLOCK_SIZE = 64 * 1024
FORCE_UPDATE = os.environ.get('FORCE_SIGNATURE_UPDATE', 'false').lower() == 'true'


class SkipSource(RuntimeError):
    pass


def add_cacert(cert: str) -> None:
    # Add certificate to requests
    cafile = certifi.where()
    with open(cafile, 'a') as ca_editor:
        ca_editor.write(f"\n{cert}")


def filter_downloads(update_directory, pattern, default_pattern=".*") -> List[Tuple[str, str]]:
    f_files = []
    if not pattern:
        # Regex will either match on the filename, directory, or filepath, either with default or given pattern for source
        pattern = default_pattern
    for path_in_dir, subdirs, files in os.walk(update_directory):
        for filename in files:
            filepath = os.path.join(update_directory, path_in_dir, filename)
            if re.match(pattern, filepath) or re.match(pattern, filename):
                f_files.append((filepath, get_sha256_for_file(filepath)))
        for subdir in subdirs:
            dirpath = f'{os.path.join(update_directory, path_in_dir, subdir)}/'
            if re.match(pattern, dirpath):
                f_files.append((dirpath, get_sha256_for_file(make_archive(subdir, 'tar', root_dir=dirpath))))

    return f_files


def url_download(source: Dict[str, Any], previous_update: int = None,
                 logger=None, output_dir: str = None) -> List[Tuple[str, str]]:
    """

    :param source:
    :param previous_update:
    :return:
    """
    name = source['name']
    uri = source['uri']
    pattern = source.get('pattern', None)
    username = source.get('username', None)
    password = source.get('password', None)
    ca_cert = source.get('ca_cert', None)
    ignore_ssl_errors = source.get('ssl_ignore_errors', False)
    auth = (username, password) if username and password else None

    proxy = source.get('proxy', None)
    headers_list = source.get('headers', [])
    headers = {}
    [headers.update({header['name']: header['value']}) for header in headers_list]

    logger.info(f"{name} source is configured to {'ignore SSL errors' if ignore_ssl_errors else 'verify SSL'}.")
    if ca_cert:
        logger.info("A CA certificate has been provided with this source.")
        add_cacert(ca_cert)

    # Create a requests session
    session = requests.Session()
    session.verify = not ignore_ssl_errors

    # Let https requests go through proxy
    if proxy:
        os.environ['https_proxy'] = proxy

    try:
        if isinstance(previous_update, str):
            previous_update = iso_to_epoch(previous_update)

        # Check the response header for the last modified date
        response = session.head(uri, auth=auth, headers=headers)
        last_modified = response.headers.get('Last-Modified', None)
        if last_modified:
            # Convert the last modified time to epoch
            last_modified = time.mktime(time.strptime(last_modified, "%a, %d %b %Y %H:%M:%S %Z"))

            # Compare the last modified time with the last updated time
            if previous_update and last_modified <= previous_update and not FORCE_UPDATE:
                # File has not been modified since last update, do nothing
                raise SkipSource()

        if previous_update:
            previous_update = time.strftime("%a, %d %b %Y %H:%M:%S %Z", time.gmtime(previous_update))
            if headers:
                headers['If-Modified-Since'] = previous_update
            else:
                headers = {'If-Modified-Since': previous_update}

        response = session.get(uri, auth=auth, headers=headers)

        # Check the response code
        if response.status_code == requests.codes['not_modified'] and not FORCE_UPDATE:
            # File has not been modified since last update, do nothing
            raise SkipSource()
        elif response.ok:
            if not os.path.exists(output_dir):
                os.makedirs(output_dir)

            file_name = os.path.basename(urlparse(uri).path)
            file_path = os.path.join(output_dir, file_name)
            with open(file_path, 'wb') as f:
                for content in response.iter_content(BLOCK_SIZE):
                    f.write(content)

            # Clear proxy setting
            if proxy:
                del os.environ['https_proxy']

            if file_name.endswith('tar.gz') or file_name.endswith('zip'):
                extract_dir = os.path.join(output_dir, name)
                shutil.unpack_archive(file_path, extract_dir=extract_dir)

                return filter_downloads(extract_dir, pattern)
            else:
                return [(file_path, get_sha256_for_file(file_path))]
        else:
            logger.warning(f"Download not successful: {response.content}")
            return []

    except SkipSource:
        # Raise to calling function for handling
        raise
    except Exception as e:
        # Catch all other types of exceptions such as ConnectionError, ProxyError, etc.
        logger.warning(str(e))
        exit()
    finally:
        # Close the requests session
        session.close()


def git_clone_repo(source: Dict[str, Any], previous_update: int = None, default_pattern: str = "*",
                   logger=None, output_dir: str = None) -> List[Tuple[str, str]]:
    name = source['name']
    url = source['uri']
    pattern = source.get('pattern', None)
    key = source.get('private_key', None)
    username = source.get('username', None)
    password = source.get('password', None)

    ignore_ssl_errors = source.get("ssl_ignore_errors", False)
    ca_cert = source.get("ca_cert")
    proxy = source.get('proxy', None)
    auth = f'{username}:{password}@' if username and password else None

    git_config = None
    git_env = {}

    if ignore_ssl_errors:
        git_env['GIT_SSL_NO_VERIFY'] = '1'

    # Let https requests go through proxy
    if proxy:
        os.environ['https_proxy'] = proxy

    if ca_cert:
        logger.info("A CA certificate has been provided with this source.")
        add_cacert(ca_cert)
        git_env['GIT_SSL_CAINFO'] = certifi.where()

    if auth:
        logger.info("Credentials provided for auth..")
        url = re.sub(r'^(?P<scheme>https?://)', fr'\g<scheme>{auth}', url)

    clone_dir = os.path.join(output_dir, name)
    if os.path.exists(clone_dir):
        shutil.rmtree(clone_dir)

    with tempfile.NamedTemporaryFile() as git_ssh_identity_file:
        if key:
            logger.info(f"key found for {url}")
            # Save the key to a file
            git_ssh_identity_file.write(key.encode())
            git_ssh_identity_file.seek(0)
            os.chmod(git_ssh_identity_file.name, 0o0400)

            git_ssh_cmd = f"ssh -oStrictHostKeyChecking=no -i {git_ssh_identity_file.name}"
            git_env['GIT_SSH_COMMAND'] = git_ssh_cmd

        repo = Repo.clone_from(url, clone_dir, env=git_env, git_config=git_config)

        # Check repo last commit
        if previous_update:
            if isinstance(previous_update, str):
                previous_update = iso_to_epoch(previous_update)
            for c in repo.iter_commits():
                if c.committed_date < previous_update and not FORCE_UPDATE:
                    raise SkipSource()
                break

    # Clear proxy setting
    if proxy:
        del os.environ['https_proxy']

    return filter_downloads(clone_dir, pattern, default_pattern)
