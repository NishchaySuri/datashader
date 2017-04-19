#!/usr/bin/env python

from __future__ import print_function, absolute_import, division

# -*- coding: utf-8 -*-
"""
Copyright (c) 2011, Kenneth Reitz <me@kennethreitz.com>

Permission to use, copy, modify, and/or distribute this software for any
purpose with or without fee is hereby granted, provided that the above
copyright notice and this permission notice appear in all copies.

THE SOFTWARE IS PROVIDED "AS IS" AND THE AUTHOR DISCLAIMS ALL WARRANTIES
WITH REGARD TO THIS SOFTWARE INCLUDING ALL IMPLIED WARRANTIES OF
MERCHANTABILITY AND FITNESS. IN NO EVENT SHALL THE AUTHOR BE LIABLE FOR
ANY SPECIAL, DIRECT, INDIRECT, OR CONSEQUENTIAL DAMAGES OR ANY DAMAGES
WHATSOEVER RESULTING FROM LOSS OF USE, DATA OR PROFITS, WHETHER IN AN
ACTION OF CONTRACT, NEGLIGENCE OR OTHER TORTIOUS ACTION, ARISING OUT OF
OR IN CONNECTION WITH THE USE OR PERFORMANCE OF THIS SOFTWARE.

clint.textui.progress
~~~~~~~~~~~~~~~~~

This module provides the progressbar functionality.

"""
from collections import OrderedDict
from os import path
import glob
import os
import subprocess
import sys
import tarfile
import time
import zipfile

import yaml
try:
    import requests
except ImportError:
    print('this download script requires the requests module: conda install requests')
    sys.exit(1)

from py7zlib import Archive7z

STREAM = sys.stderr

BAR_TEMPLATE = '%s[%s%s] %i/%i - %s\r'
MILL_TEMPLATE = '%s %s %i/%i\r'

DOTS_CHAR = '.'
BAR_FILLED_CHAR = '#'
BAR_EMPTY_CHAR = ' '
MILL_CHARS = ['|', '/', '-', '\\']

# How long to wait before recalculating the ETA
ETA_INTERVAL = 1
# How many intervals (excluding the current one) to calculate the simple moving
# average
ETA_SMA_WINDOW = 9


class Bar(object):
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.done()
        return False  # we're not suppressing exceptions

    def __init__(self, label='', width=32, hide=None, empty_char=BAR_EMPTY_CHAR,
                 filled_char=BAR_FILLED_CHAR, expected_size=None, every=1):
        '''Bar is a class for printing the status of downloads'''
        self.label = label
        self.width = width
        self.hide = hide
        # Only show bar in terminals by default (better for piping, logging etc.)
        if hide is None:
            try:
                self.hide = not STREAM.isatty()
            except AttributeError:  # output does not support isatty()
                self.hide = True
        self.empty_char =    empty_char
        self.filled_char =   filled_char
        self.expected_size = expected_size
        self.every =         every
        self.start =         time.time()
        self.ittimes =       []
        self.eta =           0
        self.etadelta =      time.time()
        self.etadisp =       self.format_time(self.eta)
        self.last_progress = 0
        if (self.expected_size):
            self.show(0)

    def show(self, progress, count=None):
        if count is not None:
            self.expected_size = count
        if self.expected_size is None:
            raise Exception("expected_size not initialized")
        self.last_progress = progress
        if (time.time() - self.etadelta) > ETA_INTERVAL:
            self.etadelta = time.time()
            self.ittimes = \
                self.ittimes[-ETA_SMA_WINDOW:] + \
                    [-(self.start - time.time()) / (progress+1)]
            self.eta = \
                sum(self.ittimes) / float(len(self.ittimes)) * \
                (self.expected_size - progress)
            self.etadisp = self.format_time(self.eta)
        x = int(self.width * progress / self.expected_size)
        if not self.hide:
            if ((progress % self.every) == 0 or      # True every "every" updates
                (progress == self.expected_size)):   # And when we're done
                STREAM.write(BAR_TEMPLATE % (
                    self.label, self.filled_char * x,
                    self.empty_char * (self.width - x), progress,
                    self.expected_size, self.etadisp))
                STREAM.flush()

    def done(self):
        self.elapsed = time.time() - self.start
        elapsed_disp = self.format_time(self.elapsed)
        if not self.hide:
            # Print completed bar with elapsed time
            STREAM.write(BAR_TEMPLATE % (
                self.label, self.filled_char * self.width,
                self.empty_char * 0, self.last_progress,
                self.expected_size, elapsed_disp))
            STREAM.write('\n')
            STREAM.flush()

    def format_time(self, seconds):
        return time.strftime('%H:%M:%S', time.gmtime(seconds))


def bar(it, label='', width=32, hide=None, empty_char=BAR_EMPTY_CHAR,
        filled_char=BAR_FILLED_CHAR, expected_size=None, every=1):
    """Progress iterator. Wrap your iterables with it."""

    count = len(it) if expected_size is None else expected_size

    with Bar(label=label, width=width, hide=hide, empty_char=BAR_EMPTY_CHAR,
             filled_char=BAR_FILLED_CHAR, expected_size=count, every=every) \
            as bar:
        for i, item in enumerate(it):
            yield item
            bar.show(i + 1)


def ordered_load(stream, Loader=yaml.Loader, object_pairs_hook=OrderedDict):
    class OrderedLoader(Loader):
        pass
    def construct_mapping(loader, node):
        loader.flatten_mapping(node)
        return object_pairs_hook(loader.construct_pairs(node))
    OrderedLoader.add_constructor(
        yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
        construct_mapping)
    return yaml.load(stream, OrderedLoader)


class DirectoryContext(object):
    """
    Context Manager for changing directories
    """
    def __init__(self, path):
        self.old_dir = os.getcwd()
        self.new_dir = path

    def __enter__(self):
        os.chdir(self.new_dir)

    def __exit__(self, *args):
        os.chdir(self.old_dir)


def _url_to_binary_write(url, output_path, title):
    '''Given a url, output_path and title,
    write the contents of a requests get operation to
    the url in binary mode and print the title of operation'''
    print('Downloading {0}'.format(title))
    resp = requests.get(url, stream=True)
    try:
        with open(output_path, 'wb') as f:
            total_length = int(resp.headers.get('content-length'))
            for chunk in bar(resp.iter_content(chunk_size=1024), expected_size=(total_length/1024) + 1, every=1000):
                if chunk:
                    f.write(chunk)
                    f.flush()
    except:
        # Don't leave a half-written zip file
        if path.exists(output_path):
            os.remove(output_path)
        raise


def _unzip_7z(fname):
    '''This function will decompress a 7zip file, typically
    a file ending in .7z (see lidar example in datasets.yml).
    The lidar example downloads 7zips and extracts text files
    (.gnd) files with this function'''
    try:
        arc = Archive7z(open(fname, 'rb'))
    except:
        print('FAILED ON 7Z', fname)
        raise
    fnames = arc.filenames
    files = arc.files
    data_dir = os.path.dirname(fname)
    for fn, fi in zip(fnames, files):
        gnd = path.join(data_dir, os.path.basename(fn))
        if not os.path.exists(os.path.dirname(gnd)):
            os.mkdir(os.path.dirname(gnd))
        with open(gnd, 'w') as f:
            f.write(fi.read().decode())


def _extract_downloaded_archive(output_path):
    '''Extract a local archive, e.g. zip or tar, then
    delete the archive'''
    if output_path.endswith("tar.gz"):
        with tarfile.open(output_path, "r:gz") as tar:
            tar.extractall()
    elif output_path.endswith("tar"):
        with tarfile.open(output_path, "r:") as tar:
            tar.extractall()
    elif output_path.endswith("tar.bz2"):
        with tarfile.open(output_path, "r:bz2") as tar:
            tar.extractall()
    elif output_path.endswith("zip"):
        with zipfile.ZipFile(output_path, 'r') as zipf:
            zipf.extractall()
    elif output_path.endswith('7z'):
        _unzip_7z(output_path)
    os.remove(output_path)


def _process_dataset(dataset, output_dir, here):
    '''Process each download spec in datasets.yml

    Typically each dataset list entry in the yml has
    "files" and "url" and "title" keys/values to show
    local files that must be present / extracted from
    a decompression of contents downloaded from the url.

    If a dataset has a "tag" key then it is expected
    a special case is handled in _handle_special_cases
    (see the lidar dataset for an example)'''

    if not path.exists(output_dir):
        os.makedirs(output_dir)

    with DirectoryContext(output_dir) as d:
        requires_download = False
        for f in dataset.get('files', []):
            if not path.exists(f):
                requires_download = True
                break

        if not requires_download:
            print('Skipping {0}'.format(dataset['title']))
            return
        tag = dataset.get('tag')
        if not tag:
            output_path = path.split(dataset['url'])[1]
            _url_to_binary_write(dataset['url'], output_path, dataset['title'])
            _extract_downloaded_archive(output_path)
        else:
            _handle_special_cases(here, **dataset)


def _lidar_url_to_files(here, url, files):
    '''The lidar dataset has a 25 ca. 130 MB
    7zips to download and unpack.  This func
    modifies a URL pattern'''
    urls = [url.replace('.html', '/' + _.strip())
            for _ in files]
    compressed = [path.join(here, 'data', os.path.basename(fname))
                  for fname in files]
    decompressed = [fname.replace('7z', 'gnd')
                    for fname in files]
    return urls, compressed, decompressed


def _handle_special_cases(here, **dataset):
    '''Some datasets have a number of medium sized compressed files
    that need to be downloaded but most have a single zip archive
    that is unpacked to many files.  the lidar dataset uses this
    logic. Each special case dataset must have a "tag" to define which
    special case it is, e.g. "lidar" below.
    '''
    tag = dataset['tag']
    files = dataset.get('files') or []
    url = dataset['url']
    title = dataset['title']
    title_fmt = title + ' {} of {}'
    if tag == 'lidar':
        urls, compressed, decompressed = _lidar_url_to_files(here, url, files)
        srcs_targets = zip(urls, compressed, decompressed)
    elif tag:
        raise NotImplementedError('For a many-file dataset, see the example in the lidar dataset of datasets.yml and define a special case here if needed')
    for idx, (url, output_path, decompressed) in enumerate(srcs_targets):
        running_title = title_fmt.format(idx + 1, len(urls))
        if os.path.exists(decompressed):
            print('Skipping {0}'.format(running_title))
            continue
        _url_to_binary_write(url, output_path, running_title)
        _extract_downloaded_archive(output_path)


def main():
    '''Download each dataset specified by datasets.yml in this directory'''
    here = contrib_dir = path.abspath(path.join(path.split(__file__)[0]))
    info_file = path.join(here, 'datasets.yml')
    with open(info_file) as f:
        info = ordered_load(f.read())
        for topic, downloads in info.items():
            output_dir = path.join(here, topic)
            for d in downloads:
                _process_dataset(d, output_dir, here)

if __name__ == '__main__':
    main()
