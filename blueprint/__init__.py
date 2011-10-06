from ConfigParser import ConfigParser
from collections import defaultdict
import copy
import json
import logging
import os
import os.path
import re
import subprocess
import time
import urllib

# This must be called early - before the rest of the blueprint library loads.
logging.basicConfig(format='# [blueprint] %(message)s',
                    level=logging.INFO)

import context_managers
import git
import managers
import util


DEFAULTS = {'io': {'server': 'https://devstructure.com'}}


cfg = ConfigParser()
for section, options in DEFAULTS.iteritems():
    cfg.add_section(section)
    for option, value in options.iteritems():
        cfg.set(section, option, str(value))
legacy_cfg = ConfigParser()
legacy_cfg.read(['/etc/blueprint-io.cfg',
                 os.path.expanduser('~/.blueprint-io.cfg')])
for section in legacy_cfg.sections():
    for option in legacy_cfg.options(section):
        cfg.set('io', option, legacy_cfg.get(section, option))
del legacy_cfg
cfg.read(['/etc/blueprint.cfg',
          os.path.expanduser('~/.blueprint.cfg')])


class NameError(ValueError):
    pass


class NotFoundError(KeyError):
    pass


class Blueprint(dict):

    DISCLAIMER = """#
# Automatically generated by blueprint(7).  Edit at your own risk.
#
"""

    @classmethod
    def destroy(cls, name):
        """
        Destroy the named blueprint.
        """
        if not os.path.isdir(git.repo()):
            raise NotFoundError(name)
        try:
            git.git('branch', '-D', name)
        except:
            raise NotFoundError(name)

    @classmethod
    def iter(cls):
        """
        Yield the name of each blueprint.
        """
        if not os.path.isdir(git.repo()):
            return
        status, stdout = git.git('branch')
        for line in stdout.splitlines():
            yield line.strip()

    @classmethod
    def load(cls, f, name=None):
        """
        Instantiate and return a Blueprint object from a file-like object
        from which valid blueprint JSON may be read.
        """
        b = cls()
        b.name = name
        b.update(json.load(f))
        return b

    @classmethod
    def loads(cls, s, name=None):
        """
        Instantiate and return a Blueprint object from a string containing
        valid blueprint JSON.
        """
        b = cls()
        b.name = name
        b.update(json.loads(s))
        return b

    def __init__(self, name=None, commit=None, create=False):
        """
        Construct a blueprint in the new format in a backwards-compatible
        manner.
        """
        self.name = name
        self._commit = commit

        # Create a new blueprint object and populate it based on this server.
        if create:
            super(Blueprint, self).__init__()
            import backend
            for funcname in backend.__all__:
                getattr(backend, funcname)(self)
            import services
            services.services(self)

        # Create a blueprint from a Git repository.
        elif name is not None:
            git.init()
            if self._commit is None:
                self._commit = git.rev_parse('refs/heads/{0}'.format(name))
                if self._commit is None:
                    raise NotFoundError(name)
            tree = git.tree(self._commit)
            blob = git.blob(tree, 'blueprint.json')
            content = git.content(blob)
            super(Blueprint, self).__init__(**json.loads(content))

        # Create an empty blueprint object to be filled in later.
        else:
            super(Blueprint, self).__init__()

    def __sub__(self, other):
        """
        Subtracting one blueprint from another allows blueprints to remain
        free of superfluous packages from the base installation.  It takes
        three passes through the package tree.  The first two remove
        superfluous packages and the final one accounts for some special
        dependencies by adding them back to the tree.
        """
        b = copy.deepcopy(self)

        # Compare file contents and metadata.  Keep files that differ.
        for pathname, file in self.files.iteritems():
            if other.files.get(pathname, {}) == file:
                del b.files[pathname]

        # The first pass removes all duplicate packages that are not
        # themselves managers.  Allowing multiple versions of the same
        # packages complicates things slightly.  For each package, each
        # version that appears in the other blueprint is removed from
        # this blueprint.  After that is finished, this blueprint is
        # normalized.  If no versions remain, the package is removed.
        def package(manager, package, version):
            if package in b.packages:
                return
            try:
                b_packages = b.packages[manager]
            except KeyError:
                return
            if manager in b_packages:
                return
            if package not in b_packages:
                return
            try:
                b_versions = b_packages[package]
            except KeyError:
                return
            try:
                del b_versions[b_versions.index(version)]
            except ValueError:
                pass
            if 0 == len(b_versions):
                del b_packages[package]
            else:
                b_packages[package] = b_versions
        other.walk(package=package)

        # The second pass removes managers that manage no packages, a
        # potential side-effect of the first pass.  This step must be
        # applied repeatedly until the blueprint reaches a steady state.
        def package(manager, package, version):
            if package not in b.packages:
                return
            if 0 == len(b.packages[package]):
                del b.packages[package]
                del b.packages[self.managers[package]][package]
        while 1:
            l = len(b.packages)
            other.walk(package=package)
            if len(b.packages) == l:
                break

        # The third pass adds back special dependencies like `ruby*-dev`.
        # It isn't apparent from the rules above that a manager like RubyGems
        # needs more than just itself to function.  In some sense, this might
        # be considered a missing dependency in the Debian archive but in
        # reality it's only _likely_ that you need `ruby*-dev` to use
        # `rubygems*`.
        def after_packages(manager):
            if manager not in b.packages:
                return

            deps = {r'^python(\d+(?:\.\d+)?)$': ['python{0}',
                                                 'python{0}-dev',
                                                 'python',
                                                 'python-devel'],
                    r'^ruby(\d+\.\d+(?:\.\d+)?)$': ['ruby{0}-dev'],
                    r'^rubygems(\d+\.\d+(?:\.\d+)?)$': ['ruby{0}',
                                                        'ruby{0}-dev',
                                                        'ruby',
                                                        'ruby-devel']}

            for pattern, packages in deps.iteritems():
                match = re.search(pattern, manager)
                if match is None:
                    continue
                for package in packages:
                    package = package.format(match.group(1))
                    for managername in ('apt', 'yum'):
                        mine = self.packages.get(managername, {}).get(package,
                                                                      None)
                        if mine is not None:
                            b.packages[managername][package] = mine
        other.walk(after_packages=after_packages)

        # Compare service metadata.  Keep services that differ.
        for manager, services in self.services.iteritems():
            for service, deps in services.iteritems():
                if other.services.get(manager, {}).get(service, {}) == deps:
                    del b.services[manager][service]
            if 0 == len(b.services[manager]):
                del b.services[manager]

        # Compare source tarball filenames, which indicate their content.
        # Keep source tarballs that differ.
        for dirname, filename in self.sources.iteritems():
            if other.sources.get(dirname, '') == filename:
                del b.sources[dirname]

        return b

    def get_name(self):
        return self._name
    def set_name(self, name):
        """
        Validate and set the blueprint name.
        """
        if name is not None and re.search(r'^$|^-$|[/ \t\r\n]', name):
            raise NameError('invalid blueprint name')
        self._name = name
    name = property(get_name, set_name)

    def get_arch(self):
        if 'arch' not in self:
            self['arch'] = None
        return self['arch']
    def set_arch(self, arch):
        self['arch'] = arch
    arch = property(get_arch, set_arch)

    @property
    def files(self):
        if 'files' not in self:
            self['files'] = defaultdict(dict)
        return self['files']

    @property
    def managers(self):
        """
        Build a hierarchy of managers for easy access when declaring
        dependencies.
        """
        if hasattr(self, '_managers'):
            return self._managers
        self._managers = {'apt': None, 'yum': None}

        def package(manager, package, version):
            if package in self.packages and manager != package:
                self._managers[package] = manager

        self.walk(package=package)
        return self._managers

    @property
    def packages(self):
        if 'packages' not in self:
            self['packages'] = defaultdict(lambda: defaultdict(set))
        return self['packages']

    @property
    def services(self):
        if 'services' not in self:
            self['services'] = defaultdict(lambda: defaultdict(dict))
        return self['services']

    @property
    def sources(self):
        if 'sources' not in self:
            self['sources'] = defaultdict(dict)
        return self['sources']

    def add_file(self, pathname, **kwargs):
        """
        Create a file resource.
        """
        self.files[pathname] = kwargs

    def add_package(self, manager, package, version):
        """
        Create a package resource.
        """
        self.packages[manager][package].add(version)

    def add_service(self, manager, service):
        """
        Create a service resource which depends on given files and packages.
        """

        # AWS cfn-init respects the enable and ensure parameters like Puppet
        # does.  Blueprint provides these parameters for interoperability.
        self.services[manager].setdefault(service, {'enable': True,
                                                    'ensureRunning': True})

    def add_service_file(self, manager, service, *args):
        """
        Add file dependencies to a service resource.
        """
        if 0 == len(args):
            return
        s = self.services[manager][service].setdefault('files', set())
        for dirname in args:
            s.add(dirname)

    def add_service_package(self, manager, service, package_manager, *args):
        """
        Add package dependencies to a service resource.
        """
        if 0 == len(args):
            return
        d = self.services[manager][service].setdefault('packages',
                                                       defaultdict(set))
        for package in args:
            d[package_manager].add(package)

    def add_service_source(self, manager, service, *args):
        """
        Add source tarball dependencies to a service resource.
        """
        if 0 == len(args):
            return
        s = self.services[manager][service].setdefault('sources', set())
        for dirname in args:
            s.add(dirname)

    def add_source(self, dirname, filename):
        """
        Create a source tarball resource.
        """
        self.sources[dirname] = filename

    def commit(self, message=''):
        """
        Create a new revision of this blueprint in the local Git repository.
        Include the blueprint JSON and any source archives referenced by
        the JSON.
        """
        git.init()
        refname = 'refs/heads/{0}'.format(self.name)
        parent = git.rev_parse(refname)

        # Start with an empty index every time.  Specifically, clear out
        # source tarballs from the parent commit.
        if parent is not None:
            for mode, type, sha, pathname in git.ls_tree(git.tree(parent)):
                git.git('update-index', '--force-remove', pathname)

        # Add `blueprint.json` to the index.
        f = open('blueprint.json', 'w')
        f.write(self.dumps())
        f.close()
        git.git('update-index', '--add', os.path.abspath('blueprint.json'))

        # Add source tarballs to the index.
        for filename in self.sources.itervalues():
            git.git('update-index', '--add', os.path.abspath(filename))

        # Add `/etc/blueprintignore` and `~/.blueprintignore` to the index.
        # Since adding extra syntax to this file, it no longer makes sense
        # to store it as `.gitignore`.
        f = open('blueprintignore', 'w')
        for pathname in ('/etc/blueprintignore',
                         os.path.expanduser('~/.blueprintignore')):
            try:
                f.write(open(pathname).read())
            except IOError:
                pass
        f.close()
        git.git('update-index', '--add', os.path.abspath('blueprintignore'))

        # Write the index to Git's object store.
        tree = git.write_tree()

        # Write the commit and update the tip of the branch.
        self._commit = git.commit_tree(tree, message, parent)
        git.git('update-ref', refname, self._commit)

    def dumps(self):
        """
        Return a JSON serialization of this blueprint.  Make a best effort
        to prevent variance from run-to-run.  Remove superfluous empty keys.
        """
        if 'arch' in self and self['arch'] is None:
            del self['arch']
        for key in ['files', 'packages', 'sources']:
            if key in self and 0 == len(self[key]):
                del self[key]
        return util.json_dumps(self)

    def puppet(self, relaxed=False):
        """
        Generate Puppet code.
        """
        import frontend.puppet
        return frontend.puppet.puppet(self, relaxed)

    def chef(self, relaxed=False):
        """
        Generate Chef code.
        """
        import frontend.chef
        return frontend.chef.chef(self, relaxed)

    def sh(self,
           relaxed=False,
           server='https://devstructure.com',
           secret=None):
        """
        Generate shell code.
        """
        import frontend.sh
        return frontend.sh.sh(self, relaxed, server, secret)

    def blueprintignore(self):
        """
        Return the blueprint's blueprintignore file.  Prior to v3.0.9 this
        file was stored as .blueprintignore in the repository.  Prior to
        v3.0.4 this file was stored as .gitignore in the repository.
        """
        tree = git.tree(self._commit)
        blob = git.blob(tree, 'blueprintignore')
        if blob is None:
            blob = git.blob(tree, '.blueprintignore')
        if blob is None:
            blob = git.blob(tree, '.gitignore')
        import ignore
        if blob is None:
            return ignore.Rules('')
        content = git.content(blob)
        if content is None:
            return ignore.Rules('')
        return ignore.Rules(content)

    def walk(self, **kwargs):
        import walk
        walk.walk(self, **kwargs)
