# Copyright 2017 The Bazel Authors. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""The whl modules defines classes for interacting with Python packages."""

import argparse
import json
import os
import pkg_resources
import re
import shutil
import zipfile


class Wheel(object):

  def __init__(self, path):
    self._path = path

  def path(self):
    return self._path

  def basename(self):
    return os.path.basename(self.path())

  def distribution(self):
    # See https://www.python.org/dev/peps/pep-0427/#file-name-convention
    parts = self.basename().split('-')
    return parts[0]

  def version(self):
    # See https://www.python.org/dev/peps/pep-0427/#file-name-convention
    parts = self.basename().split('-')
    return parts[1]

  def repository_name(self):
    # Returns the canonical name of the Bazel repository for this package.
    canonical = 'pypi__{}_{}'.format(self.distribution(), self.version())
    # Escape any illegal characters with underscore.
    return re.sub('[-.+]', '_', canonical)

  def _dist_info(self):
    # Return the name of the dist-info directory within the .whl file.
    # e.g. google_cloud-0.27.0-py2.py3-none-any.whl ->
    #      google_cloud-0.27.0.dist-info
    return '{}-{}.dist-info'.format(self.distribution(), self.version())

  def _data(self):
    # Return the name of the data directory within the .whl file.
    # e.g. google_cloud-0.27.0-py2.py3-none-any.whl ->
    #      google_cloud-0.27.0.data
    return '{}-{}.data'.format(self.distribution(), self.version())

  def metadata(self):
    # Extract the structured data from metadata.json in the WHL's dist-info
    # directory.
    with zipfile.ZipFile(self.path(), 'r') as whl:
      # first check for metadata.json
      try:
        with whl.open(self._dist_info() + '/metadata.json') as f:
          return json.loads(f.read().decode("utf-8"))
      except KeyError:
          pass
      # fall back to METADATA file (https://www.python.org/dev/peps/pep-0427/)
      with whl.open(self._dist_info() + '/METADATA') as f:
        return Wheel._parse_metadata(f.read().decode("utf-8"))

  def name(self):
    return self.metadata().get('name')

  def dependencies(self, extra=None):
    """Access the dependencies of this Wheel.

    Args:
      extra: if specified, include the additional dependencies
            of the named "extra".

    Yields:
      the names of requirements from the metadata.json
    """
    # TODO(mattmoor): Is there a schema to follow for this?
    run_requires = self.metadata().get('run_requires', [])
    for requirement in run_requires:
      if requirement.get('extra') != extra:
        # Match the requirements for the extra we're looking for.
        continue
      marker = requirement.get('environment')
      if marker and not pkg_resources.evaluate_marker(marker):
        # The current environment does not match the provided PEP 508 marker,
        # so ignore this requirement.
        continue
      requires = requirement.get('requires', [])
      for entry in requires:
        # Strip off any trailing versioning data.
        parts = re.split('[ ><=()]', entry)
        yield parts[0]

  def extras(self):
    return self.metadata().get('extras', [])

  def expand(self, directory):
    with zipfile.ZipFile(self.path(), 'r') as whl:
      whl.extractall(directory)

    # Move files under data/purelib to the top level.
    purelib = os.path.join(directory, self._data(), 'purelib')
    if os.path.exists(purelib):
      for f in os.listdir(purelib):
        shutil.move(os.path.join(purelib, f), directory)

  def file_names(self):
    purelib = os.path.join(self._data(), 'purelib')

    def rename_purelib(file_name):
      if file_name.startswith(purelib):
        return file_name[len(purelib) + 1:]
      return file_name

    with zipfile.ZipFile(self.path(), 'r') as whl:
      return [rename_purelib(file_name) for file_name in whl.namelist()]

  # _parse_metadata parses METADATA files according to https://www.python.org/dev/peps/pep-0314/
  @staticmethod
  def _parse_metadata(content):
    # TODO: handle fields other than just name
    name_pattern = re.compile('^Name: (.*)', flags=re.MULTILINE)
    name = name_pattern.search(content).group(1)

    extra_pattern = re.compile('^Provides-Extra: (.*)', flags=re.MULTILINE)
    extras = sorted(set(extra_pattern.findall(content)))

    requires_pattern = re.compile('^Requires-Dist: (.*)', flags=re.MULTILINE)
    requires = []
    for req in requires_pattern.findall(content):
      requires.extend(pkg_resources.parse_requirements(req))

    def reqs_for_extra(extra):
      for req in requires:
        if not req.marker or req.marker.evaluate({'extra': extra}):
          yield req

    common = frozenset(reqs_for_extra(None))
    require_map = {
      None: list(common),
    }

    for extra in extras:
      s_extra = pkg_resources.safe_extra(extra.strip())
      require_map[s_extra] = list(frozenset(reqs_for_extra(extra)) - common)

    # Key: (extra, marker)
    run_requires = {}
    for extra, reqs in require_map.items():
      for req in reqs:
        key = (extra, str(req.marker))
        entry = run_requires.get(key)
        if not entry:
          entry = {
            'requires': [],
            'extra': extra,
            'marker': str(req.marker) if req.marker else None,
          }
          run_requires[key] = entry
        entry['requires'].append(req.key)
    run_requires = sorted(run_requires.values(),
                          key=lambda r: (r['extra'] or '', r['marker'] or ''))
    for entry in run_requires:
      entry['requires'] = sorted(entry['requires'])

    return {
      'name': name,
      'extras': extras,
      'run_requires': run_requires,
    }


parser = argparse.ArgumentParser(
    description='Unpack a WHL file as a py_library.')

parser.add_argument('--whl', action='store',
                    help=('The .whl file we are expanding.'))

parser.add_argument('--requirements', action='store',
                    help='The pip_import from which to draw dependencies.')

parser.add_argument('--directory', action='store', default='.',
                    help='The directory into which to expand things.')

parser.add_argument('--extras', action='append',
                    help='The set of extras for which to generate library targets.')

def main():
  args = parser.parse_args()
  whl = Wheel(args.whl)

  # Extract the files into the current directory
  whl.expand(args.directory)

  # Create manifest file
  with open(os.path.join(args.directory, 'manifest.bzl'), 'w') as f:
    file_names = ['"{}"'.format(file_name) for file_name in whl.file_names()]
    f.write("""
contents = [
  {files}
]
""".format(
      files=',\n  '.join(file_names)
    ))

  with open(os.path.join(args.directory, 'BUILD'), 'w') as f:
    f.write("""
package(default_visibility = ["//visibility:public"])

load("{requirements}", "requirement")

exports_files(
    glob(["**/*"], exclude=["**/*.py", "**/* *", "BUILD", "WORKSPACE"]),
)

py_library(
    name = "pkg",
    srcs = glob(["**/*.py"]),
    data = glob(["**/*"], exclude=["**/*.py", "**/* *", "BUILD", "WORKSPACE"]),
    # This makes this directory a top-level in the python import
    # search path for anything that depends on this.
    imports = ["."],
    deps = [{dependencies}],
)
{extras}""".format(
  requirements=args.requirements,
  dependencies=','.join([
    'requirement("%s")' % d
    for d in whl.dependencies()
  ]),
  extras='\n\n'.join([
    """py_library(
    name = "{extra}",
    deps = [
        ":pkg",{deps}
    ],
)""".format(extra=extra,
            deps=','.join([
                'requirement("%s")' % dep
                for dep in whl.dependencies(extra)
            ]))
    for extra in args.extras or []
  ])))

if __name__ == '__main__':
  main()
