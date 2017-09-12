from __future__ import print_function

import os
import platform
import shutil
import sys
from glob import glob
from subprocess import CalledProcessError
from subprocess import check_call
from subprocess import check_output
from time import sleep

from charms.layer.execd import execd_preinstall


def lsb_release():
    """Return /etc/lsb-release in a dict

    Based on host env, there are two methods:
    1. For Ubuntu, read /etc/lsb-release, contents in xenial 16.04 for example:
        DISTRIB_ID=Ubuntu
        DISTRIB_RELEASE=16.04
        DISTRIB_CODENAME=xenial
        DISTRIB_DESCRIPTION="Ubuntu 16.04.2 LTS"

    2. For CenTos AND RHEL, read /etc/redhat-release, one string:
        CentOS Linux release 7.3.1611 (Core)

    Returns:
        dict: Dictionary presenting the host OS. Keys are:
            1. `DISTRIB_ID`: `Ubuntu` or `CentOS`
            2. `DISTRIB_RELEASE`: eg. `16.04`, `7.3.1611`
            3. `DISTRIB_CODENAME`: eg. `xenial`, `CentOS7.3.1611`
            4. `DISTRIB_DESCRIPTION`: eg. `Ubuntu 16.04.2 LTS`
    """
    d = {}
    me = platform.linux_distribution()[0]
    if 'ubuntu' in me.lower():
        # DISTRIB_ID=Ubuntu
        # DISTRIB_RELEASE=16.04
        # DISTRIB_CODENAME=xenial
        # DISTRIB_DESCRIPTION="Ubuntu 16.04.2 LTS"
        with open('/etc/lsb-release', 'r') as lsb:
            for l in lsb:
                k, v = l.split('=')
                d[k.strip()] = v.strip()
    elif 'cent' in me.lower():
        if os.path.exists('/etc/redhat-release'):
            # http://www.binarytides.com/command-check-centos-version/
            # TODO: need verify this method reading release info of CentOS/RHEL
            # file content:
            #     CentOS Linux release 7.3.1611 (Core)
            with open('/etc/redhat-release', 'r') as lsb:
                for l in lsb:
                    if 'centos' in l.lower():
                        tmp = l.split(' ')  # split by white space
                        d['DISTRIB_ID'] = tmp[0]  # CentOS
                        d['DISTRIB_RELEASE'] = tmp[-2]  # 7.3.1611
                        d['DISTRIB_CODENAME'] = tmp[0] + tmp[-2]  # CentOS7.3.1611
                        d['DISTRIB_DESCRIPTIOIN'] = l  # original string
                        break
        # This is a fallback, if /etc/rethat-release doesn't exist
        else:
            d['DISTRIB_ID'] = 'CentOS'
            d['DISTRIB_RELEASE'] = ''  # unknown?
            d['DISTRIB_CODENAME'] = 'CentOS'
            d['DISTRIB_DESCRIPTIOIN'] = 'CentOS'

    return d


def bootstrap_charm_deps():
    """
    Set up the base charm dependencies so that the reactive system can run.
    """
    # execd must happen first, before any attempt to install packages or
    # access the network, because sites use this hook to do bespoke
    # configuration and install secrets so the rest of this bootstrap
    # and the charm itself can actually succeed. This call does nothing
    # unless the operator has created and populated $JUJU_CHARM_DIR/exec.d.
    execd_preinstall()

    # ensure that $JUJU_CHARM_DIR/bin is on the path, for helper scripts
    charm_dir = os.environ['JUJU_CHARM_DIR']
    os.environ['PATH'] += ':%s' % os.path.join(charm_dir, 'bin')
    venv = os.path.abspath('../.venv')
    vbin = os.path.join(venv, 'bin')
    vpip = os.path.join(vbin, 'pip')  # system default pip
    vpy = os.path.join(vbin, 'python')

    # ".bootstrapped" is a flag file. If it exists, meaning some other charms
    # have already done these steps so there is no need to go through
    # these steps again.
    if os.path.exists('wheelhouse/.bootstrapped'):
        activate_venv()
        return

    # determine host env
    dist = lsb_release()['DISTRIB_ID'].lower()

    # bootstrap wheelhouse
    if os.path.exists('wheelhouse'):
        with open('/root/.pydistutils.cfg', 'w') as fp:
            # make sure that easy_install also only uses the wheelhouse
            # (see https://github.com/pypa/pip/issues/410)
            fp.writelines([
                "[easy_install]\n",
                "allow_hosts = ''\n",
                "find_links = file://{}/wheelhouse/\n".format(charm_dir),
            ])

        # include packages defined in layer.yaml
        from charms import layer
        cfg = layer.options('basic')
        apt_install(cfg.get('packages', []))
        pip = 'pip'

        # need newer pip, to fix spurious Double Requirement error:
        # https://github.com/pypa/pip/issues/56
        check_call([pip, 'install', '-U', '--no-index', '-f', 'wheelhouse', 'pip'])

        # install the rest of the wheelhouse deps
        output = check_output([pip, 'install', '-U', '--no-index', '-f',
                               'wheelhouse'] + glob('wheelhouse/*'))

        os.remove('/root/.pydistutils.cfg')

        # flag us as having already bootstrapped so we don't do it again
        open('wheelhouse/.bootstrapped', 'w').close()

        # Ensure that the newly bootstrapped libs are available.
        # Note: this only seems to be an issue with namespace packages.
        # Non-namespace-package libs (e.g., charmhelpers) are available
        # without having to reload the interpreter. :/
        sys.path.append('/usr/local/lib/python2.7/dist-packages')
        reload_interpreter(vpy if cfg.get('use_venv') else sys.argv[0])


def reload_interpreter(python):
    """
    Reload the python interpreter to ensure that all deps are available.

    Newly installed modules in namespace packages sometimes seemt to
    not be picked up by Python 3.
    """
    os.execve(python, [python] + list(sys.argv), os.environ)


def apt_install(packages):
    """
    Install apt packages.

    This ensures a consistent set of options that are often missed but
    should really be set.
    """
    if isinstance(packages, (str, bytes)):
        packages = [packages]

    env = os.environ.copy()

    if 'DEBIAN_FRONTEND' not in env:
        env['DEBIAN_FRONTEND'] = 'noninteractive'

    # determine host env
    dist = lsb_release()['DISTRIB_ID'].lower()

    if 'ubuntu' in dist:
        pkg_cmd = 'apt-get'
        say_yes = '--assume-yes'
        options = ['--option=Dpkg::Options::=--force-confold', ]
        install_cmd = 'install'
    elif 'cent' in dist:
        pkg_cmd = 'yum'
        say_yes = '--assumeyes'
        options = []
        install_cmd = 'install'

    cmd = [pkg_cmd] + options + [say_yes] + [install_cmd]

    # Try cmd 3 times
    for attempt in range(3):
        try:
            check_call(cmd + packages, env=env)
        except CalledProcessError:
            if attempt == 2:  # third attempt
                raise
            sleep(5)
        else:
            break


def init_config_states():
    import yaml
    from charmhelpers.core import hookenv
    from charms.reactive import set_state
    from charms.reactive import toggle_state
    config = hookenv.config()
    config_defaults = {}
    config_defs = {}
    config_yaml = os.path.join(hookenv.charm_dir(), 'config.yaml')
    if os.path.exists(config_yaml):
        with open(config_yaml) as fp:
            config_defs = yaml.safe_load(fp).get('options', {})
            config_defaults = {key: value.get('default')
                               for key, value in config_defs.items()}
    for opt in config_defs.keys():
        if config.changed(opt):
            set_state('config.changed')
            set_state('config.changed.{}'.format(opt))
        toggle_state('config.set.{}'.format(opt), config.get(opt))
        toggle_state('config.default.{}'.format(opt),
                     config.get(opt) == config_defaults[opt])
    hookenv.atexit(clear_config_states)


def clear_config_states():
    from charmhelpers.core import hookenv, unitdata
    from charms.reactive import remove_state
    config = hookenv.config()
    remove_state('config.changed')
    for opt in config.keys():
        remove_state('config.changed.{}'.format(opt))
        remove_state('config.set.{}'.format(opt))
        remove_state('config.default.{}'.format(opt))
    unitdata.kv().flush()
