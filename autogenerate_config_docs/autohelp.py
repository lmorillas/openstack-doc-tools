#!/usr/bin/env python
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.
#
# A collection of tools for working with flags from OpenStack
# packages and documentation.
#
# For an example of usage, run this program with the -h switch.
#

# Must import this before argparse
from oslo.config import cfg

import argparse
import importlib
import os
import pickle
import re
import sys

import git
from lxml import etree
import oslo.i18n as i18n
import stevedore

from hooks import HOOKS  # noqa


EXTENSIONS = ['oslo.messaging', 'oslo.db']

IGNORE = ['trove.guestagent.datastore.postgresql.manager',
          'trove.guestagent.datastore.postgresql.service.root',
          'trove.guestagent.datastore.postgresql.service.users',
          'trove.guestagent.datastore.postgresql.service.database']

BASE_XML = '''<?xml version="1.0" encoding="UTF-8"?>
<para xmlns="http://docbook.org/ns/docbook"
  version="5.0">
<!-- Warning: Do not edit this file. It is automatically
     generated and your changes will be overwritten.
     The tool to do so lives in openstack-doc-tools repository. -->
  <table rules="all" xml:id="config_table_%(pkg)s_%(cat)s">
    <caption>Description of %(nice_cat)s configuration options</caption>
    <col width="50%%"/>
    <col width="50%%"/>
    <thead>
      <tr>
        <th>Configuration option = Default value</th>
        <th>Description</th>
      </tr>
    </thead>
    <tbody></tbody>
  </table>
</para>'''


register_re = re.compile(r'''^ +.*\.register_opts\((?P<opts>[^,)]+)'''
                         r'''(, (group=)?["'](?P<group>.*)["'])?\)''')


def git_check(repo_path):
    """Check a passed directory to verify it is a valid git repository."""

    try:
        repo = git.Repo(repo_path)
        assert repo.bare is False
        package_name = os.path.basename(repo.remotes.origin.url)
        package_name = package_name.replace('.git', '')
    except Exception:
        print("\n%s doesn't seem to be a valid git repository." % repo_path)
        print("Use the -i flag to specify the repository path.\n")
        sys.exit(1)
    return package_name


def import_modules(repo_location, package_name, verbose=0):
    """Import modules.

    Loops through the repository, importing module by module to
    populate the configuration object (cfg.CONF) created from Oslo.
    """

    # If the project uses oslo.i18n, make sure that it is initialized so that
    # the builtins contain the _ function.
    requirements = os.path.join(repo_location, 'requirements.txt')
    with open(requirements) as fd:
        with_i18n = False
        for line in fd:
            if line.startswith('oslo.i18n'):
                i18n.enable_lazy()
                i18n.install(package_name)
                with_i18n = True
                break
        if not with_i18n:
            # NOTE(gpocentek): projects didn't use oslo.i18n on havana, and
            # some imports fail because _ is not yet registered in the
            # builtins. We try to import and setup the translation tools
            # manually.
            try:
                modname = "%s.openstack.common.gettextutils" % package_name
                module = importlib.import_module(modname)
                module.install(package_name)
            except Exception:
                pass

    pkg_location = os.path.join(repo_location, package_name)
    for root, dirs, files in os.walk(pkg_location):
        skipdir = False
        for excludedir in ('tests', 'locale', 'cmd',
                           os.path.join('db', 'migration'), 'transfer'):
            if ((os.path.sep + excludedir + os.path.sep) in root or (
                    root.endswith(os.path.sep + excludedir))):
                skipdir = True
                break
        if skipdir:
            continue
        for pyfile in files:
            if pyfile.endswith('.py'):
                abs_path = os.path.join(root, pyfile)
                modfile = abs_path.split(repo_location, 1)[1]
                modname = os.path.splitext(modfile)[0].split(os.path.sep)
                modname = [m for m in modname if m != '']
                modname = '.'.join(modname)
                if modname.endswith('.__init__'):
                    modname = modname[:modname.rfind(".")]
                if modname in IGNORE:
                    continue
                try:
                    module = importlib.import_module(modname)
                    if verbose >= 1:
                        print("imported %s" % modname)
                except ImportError as e:
                    """
                    work around modules that don't like being imported in
                    this way FIXME This could probably be better, but does
                    not affect the configuration options found at this stage
                    """
                    if verbose >= 2:
                        print("Failed to import: %s (%s)" % (modname, e))
                    continue
                except cfg.DuplicateOptError as e:
                    """
                    oslo.cfg doesn't allow redefinition of a config option, but
                    we don't mind. Don't fail if this happens.
                    """
                    if verbose >= 2:
                        print(e)
                    continue
                except cfg.NoSuchGroupError as e:
                    """
                    If a group doesn't exist, we ignore the import.
                    """
                    if verbose >= 2:
                        print(e)
                    continue
                _register_runtime_opts(module, abs_path, verbose)
                _run_hook(modname)

    # All the components provide keystone token authentication, usually using a
    # pipeline. Since the auth_token options can only be discovered at runtime
    # in this configuration, we force their discovery by importing the module.
    import keystoneclient.middleware.auth_token  # noqa


def _run_hook(modname):
    try:
        HOOKS[modname]()
    except KeyError:
        pass


def _register_runtime_opts(module, abs_path, verbose):
    """Handle options not registered on module import.

    This function parses the .py files to discover calls to register_opts in
    functions and methods. It then explicitly call cfg.register_opt on each
    option to register (most of) them.
    """

    with open(abs_path) as fd:
        for line in fd:
            m = register_re.search(line)
            if not m:
                continue

            opts_var = m.group('opts')
            opts_group = m.group('group')

            # Get the object (an options list) from the opts_var string.
            # This requires parsing the string which can be of the form
            # 'foo.bar'. We treat each element as an attribute of the previous.
            register = True
            obj = module
            for item in opts_var.split('.'):
                try:
                    obj = getattr(obj, item)
                except AttributeError:
                    # FIXME(gpocentek): AttributeError is raised when a part of
                    # the opts_var string is not an actual attribute. This will
                    # need more parsing tricks.
                    register = False
                    if verbose >= 2:
                        print("Ignoring %(obj)s in %(module)s" %
                              {'obj': opts_var, 'module': module})
                    break

            if register:
                for opt in obj:
                    try:
                        cfg.CONF.register_opt(opt, opts_group)
                    except cfg.DuplicateOptError:
                        # ignore options that have already been registered
                        pass


def _sanitize_default(opt):
    """Adapts unrealistic default values."""

    # If the Oslo version is recent enough, we can use the 'sample_default'
    # attribute
    if (hasattr(opt, 'sample_default') and opt.sample_default is not None):
        return str(opt.sample_default)

    if ((type(opt).__name__ == "ListOpt") and (type(opt.default) == list)):
        return ", ".join(opt.default)

    default = str(opt.default)

    if default == os.uname()[1]:
        return 'localhost'

    if opt.name == 'bindir':
        return '/usr/local/bin'

    if opt.name == 'my_ip':
        return '10.0.0.1'

    if isinstance(opt, cfg.StrOpt) and default.strip() != default:
        return '"%s"' % default

    for pathelm in sys.path[1:]:
        if pathelm == '':
            continue
        if pathelm.endswith('/'):
            pathelm = pathelm[:-1]
        if pathelm in default:
            default = re.sub(r'%s(/sources)?' % pathelm,
                             '/usr/lib/python/site-packages', default)

    return default


class OptionsCache(object):
    def __init__(self, verbose=0):
        self._verbose = verbose
        self._opts_by_name = {}
        self._opt_names = []

        for optname in cfg.CONF._opts:
            self._add_opt(optname, 'DEFAULT', cfg.CONF._opts[optname]['opt'])

        for group in cfg.CONF._groups:
            for optname in cfg.CONF._groups[group]._opts:
                self._add_opt(group + '/' + optname, group,
                              cfg.CONF._groups[group]._opts[optname]['opt'])

        self._opt_names.sort(OptionsCache._cmpopts)

    def _add_opt(self, optname, group, opt):
        if optname in self._opts_by_name:
            if self._verbose >= 2:
                print ("Duplicate option name %s" % optname)
        else:
            opt.default = _sanitize_default(opt)

            self._opts_by_name[optname] = (group, opt)
            self._opt_names.append(optname)

    def __len__(self):
        return len(self._opt_names)

    def load_extension_options(self, module):
        # Note that options loaded this way aren't added to _opts_by_module
        loader = stevedore.named.NamedExtensionManager(
            'oslo.config.opts',
            names=(module,),
            invoke_on_load=False
        )
        for ext in loader:
            for group, opts in ext.plugin():
                for opt in opts:
                    if group is None:
                        self._add_opt(opt.name, 'DEFAULT', opt)
                    else:
                        self._add_opt(group + '/' + opt.name, group, opt)

        self._opt_names.sort(OptionsCache._cmpopts)

    def maybe_load_extensions(self, repo):
        # Use the requirements.txt of the project to guess if an oslo module
        # needs to be imported
        for ext in EXTENSIONS:
            requirements = os.path.join(repo, 'requirements.txt')
            with open(requirements) as fd:
                for line in fd:
                    if line.startswith(ext):
                        self.load_extension_options(ext)

    def get_option_names(self):
        return self._opt_names

    def get_option(self, name):
        return self._opts_by_name[name]

    @staticmethod
    def _cmpopts(x, y):
        if '/' in x and '/' in y:
            prex = x[:x.find('/')]
            prey = y[:y.find('/')]
            if prex != prey:
                return cmp(prex, prey)
            return cmp(x, y)
        elif '/' in x:
            return 1
        elif '/' in y:
            return -1
        else:
            return cmp(x, y)


def pass_through(line):
    """Whether to ignore the line."""
    return (not line.strip() or
            line.startswith('#'))


def write_docbook(package_name, options, target, verbose=0):
    """Write DocBook tables.

    Prints a docbook-formatted table for every group of options.
    """
    target = target or '../../doc/common/tables/'
    options_by_cat = {}

    with open(package_name + '.flagmappings') as f:
        for line in f:
            if pass_through(line):
                continue
            opt, categories = line.split(' ', 1)
            for category in categories.split():
                options_by_cat.setdefault(category, []).append(opt)

    package_headers = package_name + '.headers'
    category_names = {}
    for headers_file in ('shared.headers', package_headers):
        try:
            with open(headers_file) as f:
                for line in f:
                    if pass_through(line):
                        continue
                    cat, nice_name = line.split(' ', 1)
                    category_names[cat] = nice_name.strip()
        except IOError:
            print("Cannot open %s (ignored)" % headers_file)

    if not os.path.isdir(target):
        os.makedirs(target)

    for cat in options_by_cat.keys():
        parser = etree.XMLParser(remove_blank_text=True)
        if cat in category_names:
            nice_cat = category_names[cat]
        else:
            nice_cat = cat
            print("No nicename for %s" % cat)
        xml = etree.XML(BASE_XML %
                        {'pkg': package_name, 'cat': cat,
                         'nice_cat': nice_cat},
                        parser)
        tbody = xml.find(".//{http://docbook.org/ns/docbook}tbody")
        curgroup = None
        for optname in options_by_cat[cat]:
            group, option = options.get_option(optname)
            if group != curgroup:
                curgroup = group
                tr = etree.Element('tr')
                th = etree.Element('th', colspan="2")
                th.text = "[%s]" % group
                tr.append(th)
                tbody.append(tr)

            if not option.help:
                option.help = "No help text available for this option."
            default = _sanitize_default(option)

            tr = etree.Element('tr')
            tbody.append(tr)

            td = etree.Element('td')
            td.text = "%s = %s" % (option.dest, default)
            tr.append(td)

            td = etree.Element('td')
            td.text = "(%s) %s" % (type(option).__name__, option.help.strip())
            tr.append(td)

        file_path = ("%(target)s/%(package_name)s-%(cat)s.xml" %
                     {'target': target, 'package_name': package_name,
                      'cat': cat})
        with open(file_path, 'w') as fd:
            fd.write(etree.tostring(xml, pretty_print=True,
                                    xml_declaration=True,
                                    encoding="UTF-8"))


def write_docbook_rootwrap(package_name, repo, target, verbose=0):
    """Write a DocBook table for rootwrap options.

    Prints a docbook-formatted table for options in a project's
    rootwrap.conf configuration file.
    """
    target = target or '../../doc/common/tables/'

    # The sample rootwrap.conf path is not the same in all projects. It is
    # either in etc/ or in etc/<project>/, so we check both locations.
    conffile = os.path.join(repo, 'etc', package_name, 'rootwrap.conf')
    if not os.path.exists(conffile):
        conffile = os.path.join(repo, 'etc', 'rootwrap.conf')
        if not os.path.exists(conffile):
            return

    # Python's configparser doesn't pass comments through. We need those
    # to have some sort of description for the options. This simple parser
    # doesn't handle everything configparser does, but it handles everything
    # in the currentr rootwrap example conf files.
    curcomment = ''
    curgroup = 'DEFAULT'
    options = []
    for line in open(conffile):
        line = line.strip()
        if line.startswith('#'):
            if curcomment != '':
                curcomment += ' '
            curcomment += line[1:].strip()
        elif line.startswith('['):
            if line.endswith(']'):
                curgroup = line[1:-1]
                curcomment = ''
        elif '=' in line:
            key, val = line.split('=')
            options.append((curgroup, key.strip(), val.strip(), curcomment))
            curcomment = ''

    if len(options) == 0:
        return

    if not os.path.isdir(target):
        os.makedirs(target)

    parser = etree.XMLParser(remove_blank_text=True)
    xml = etree.XML(BASE_XML % {'pkg': package_name,
                                'cat': 'rootwrap',
                                'nice_cat': 'rootwrap'},
                    parser)
    tbody = xml.find(".//{http://docbook.org/ns/docbook}tbody")

    curgroup = None
    for group, optname, default, desc in options:
        if group != curgroup:
            curgroup = group
            tr = etree.Element('tr')
            th = etree.Element('th', colspan="2")
            th.text = "[%s]" % group
            tr.append(th)
            tbody.append(tr)
        if desc == '':
            desc = "No help text available for this option."

        tr = etree.Element('tr')
        tbody.append(tr)

        td = etree.Element('td')
        td.text = "%s = %s" % (optname, default)
        tr.append(td)

        td = etree.Element('td')
        td.text = desc
        tr.append(td)

    file_path = ("%(target)s/%(package_name)s-rootwrap.xml" %
                 {'target': target, 'package_name': package_name})
    with open(file_path, 'w') as fd:
        fd.write(etree.tostring(xml, pretty_print=True,
                                xml_declaration=True,
                                encoding="UTF-8"))


def create_flagmappings(package_name, options, verbose=0):
    """Create a flagmappings file.

    Create a flagmappings file. This will create a new file called
    $package_name.flagmappings with all the categories set to Unknown.
    """
    with open(package_name + '.flagmappings', 'w') as f:
        for opt in options.get_option_names():
            f.write(opt + ' Unknown\n')


def update_flagmappings(package_name, options, verbose=0):
    """Update flagmappings file.

    Update a flagmappings file, adding or removing entries as needed.
    This will create a new file $package_name.flagmappings.new with
    category information merged from the existing $package_name.flagmappings.
    """
    original_flags = {}
    with open(package_name + '.flagmappings') as f:
        for line in f:
            try:
                flag, category = line.split(' ', 1)
            except ValueError:
                flag = line.strip()
                category = "Unknown"
            original_flags.setdefault(flag, []).append(category.strip())

    updated_flags = []
    for opt in options.get_option_names():
        if len(original_flags.get(opt, [])) == 1:
            updated_flags.append((opt, original_flags[opt][0]))
            continue

        updated_flags.append((opt, 'Unknown'))

    with open(package_name + '.flagmappings.new', 'w') as f:
        for flag, category in updated_flags:
            f.write(flag + ' ' + category + '\n')

    if verbose >= 1:
        removed_flags = (set(original_flags.keys()) -
                         set([x[0] for x in updated_flags]))
        added_flags = (set([x[0] for x in updated_flags]) -
                       set(original_flags.keys()))

        print("\nRemoved Flags\n")
        for line in sorted(removed_flags, OptionsCache._cmpopts):
            print(line)

        print("\nAdded Flags\n")
        for line in sorted(added_flags, OptionsCache._cmpopts):
            print(line)


def dump_options(options):
    """Dumps the list of options with their attributes.

    This output is consumed by the diff_branches script.
    """
    print(pickle.dumps(options._opts_by_name))


def main():
    parser = argparse.ArgumentParser(
        description='Manage flag files, to aid in updating documentation.',
        usage='%(prog)s <cmd> <package> [options]')
    parser.add_argument('subcommand',
                        help='Action (create, update, verify, dump).',
                        choices=['create', 'update', 'docbook', 'dump'])
    parser.add_argument('package',
                        help='Name of the top-level package.')
    parser.add_argument('-v', '--verbose',
                        action='count',
                        default=0,
                        dest='verbose',
                        required=False,)
    parser.add_argument('-i', '--input',
                        dest='repo',
                        help='Path to a valid git repository.',
                        required=False,
                        type=str,)
    parser.add_argument('-o', '--output',
                        dest='target',
                        help='Directory or file in which data will be saved.\n'
                             'Defaults to ../../doc/common/tables/ '
                             'for "docbook".\n'
                             'Defaults to stdout for "dump"',
                        required=False,
                        type=str,)
    args = parser.parse_args()

    if args.repo is None:
        args.repo = './sources/%s' % args.package

    package_name = git_check(args.repo)

    sys.path.insert(0, args.repo)
    try:
        __import__(package_name)
    except ImportError as e:
        if args.verbose >= 1:
            print(str(e))
            print("Failed to import: %s (%s)" % (package_name, e))

    import_modules(args.repo, package_name, verbose=args.verbose)
    options = OptionsCache(verbose=args.verbose)
    options.maybe_load_extensions(args.repo)

    if args.verbose > 0:
        print("%s options imported from package %s." % (len(options),
                                                        str(package_name)))

    if args.subcommand == 'create':
        create_flagmappings(package_name, options, verbose=args.verbose)

    elif args.subcommand == 'update':
        update_flagmappings(package_name, options, verbose=args.verbose)

    elif args.subcommand == 'docbook':
        write_docbook(package_name, options, args.target, verbose=args.verbose)
        write_docbook_rootwrap(package_name, args.repo, args.target,
                               verbose=args.verbose)
    elif args.subcommand == 'dump':
        dump_options(options)


if __name__ == "__main__":
    main()
