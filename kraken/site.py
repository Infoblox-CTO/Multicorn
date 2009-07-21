# -*- coding: utf-8 -*-
# This file is part of Dyko
# Copyright © 2008-2009 Kozea
#
# This library is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This library is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with Kraken.  If not, see <http://www.gnu.org/licenses/>.

"""
Instances of the Site class are WSGI applications.  Create one for each
independent site with it’s own configuration.
"""

import os.path
import collections
import mimetypes
import re
from werkzeug.exceptions import HTTPException, NotFound

import kalamar
import koral
from kraken import utils


class Site(object):
    """
    Create a WSGI application from a site root and a kalamar configuration file.
    """
    
    def __init__(self, site_root, kalamar_conf=None):
        self.site_root = unicode(site_root)
        self.koral_site = koral.Site(site_root)
        self.kalamar_site = kalamar.Site(kalamar_conf)
        self._module_cache = {}
    
    @utils.Request.application
    def __call__(self, request):
        """WSGI entry point for every HTTP request"""
        request.koral_site = self.koral_site
        request.kalamar_site = self.kalamar_site
        try:
            if u'/__' in request.path:
                return self.handle_static_file(request)
            try:
                return self.handle_simple_template(request)
            except NotFound:
                return self.handle_python(request)
        except HTTPException, e:
            # e is also a WSGI application
            return e
    
    def handle_static_file(self, request):
        """
        Try handling a request with a static file.
        The request path is interpreted as a filename relative to the site root.
        Return a Response object or raise NotFound.
        """
        filename = os.path.join(self.site_root, *(
            part for part in request.path.split(u'/')
            if part and part != u'..'
        ))
        if not os.path.isfile(filename):
            raise NotFound
        return utils.StaticFileResponse(filename)
        
    def handle_python(self, request):
        """
        Try handling a request with a python controller
        Return a Response object or raise NotFound
        
        Exemple:
            If request.path is u'/foo/bar', this method tries the following,
            in the given order:
                - handle_request(request) in foo/bar.py
                - handle_request(request) in foo/bar/index.py
                - handle_request(request, u'') in foo/bar.py
                - handle_request(request, u'') in foo/bar/index.py
                - handle_request(request, u'bar') in foo.py
                - handle_request(request, u'bar') in foo/index.py
                - handle_request(request, u'foo/bar') in index.py
        """
        # search for foo/bar.py or foo/bar/index.py
        for suffix in (u'', u'/index'):
            module = self.load_python_module(request.path.strip(u'/') + suffix)
            if 'handle_request' in module:
                handler = module['handle_request']
                # the 2 parameters case is handled later
                if utils.arg_count(handler) == 1:
                    return handler(request)
        
        # slash-separated parts of the URL
        script_name = [part for part in request.path.split(u'/')
                       if part and part != u'..']
        path_info = collections.deque()
        while script_name:
            for suffix in (u'', u'/index'):
                module = self.load_python_module(u'/'.join(script_name) + 
                                                 suffix)
                if 'handle_request' in module:
                    handler = module['handle_request']
                    if utils.arg_count(handler) > 1:
                        return handler(request, u'/'.join(path_info))
            # take the right-most part of script_name and push it to the
            # left of path_info
            path_info.appendleft(script_name.pop())

        raise NotFound
        
    def handle_simple_template(self, request):
        """
        Try handling a request with only a template
        Return a Response object or raise NotFound
        """
        template = self.find_template(request.path)
        if not template:
            raise NotFound
        template_name, extension, engine = template
    
        # Handle a simple template
        mimetype, encoding = mimetypes.guess_type(u'_.' + extension)
        values = {'request': request}
        content = self.koral_site.engines[engine].render(template_name, values)
        return utils.Response(content, mimetype=mimetype)

    def load_python_module(self, name):
        """
        Return a dictionnary of everything defined in the module `name`
        (slash-separated path relative to the site root, without the extension).
        Return an empy dictionnary if the module does not exist.
        """
        parts = [part for part in name.split(u'/') if part and part != u'..']
        filename = os.path.join(self.site_root, *parts) + u'.py'
        if not os.path.isfile(filename):
            return {}
        mtime = os.stat(filename).st_mtime
        try:
            module, old_mtime = self._module_cache[filename]
            if mtime == old_mtime:
                return module
        except KeyError:
            pass

        namespace = {'__name__': 'kraken.site.' + '.'.join(parts)}
        execfile(filename, namespace)
        self._module_cache[filename] = (namespace, mtime)
        return namespace
        
    def find_template(self, path):
        """
        Search for an existing template named <path>/index.<type>.<engine>
        or <path>.<type>.<engine> where <engine> is a koral engine name.
        Return (template_name, type, engine) for the first one found or None.

        >>> import test.kraken
        >>> site = test.kraken.make_site()

        Directory stucture of site.site_root:
            index.html.genshi
            hello.html.jinja2
            hello/
                index.genshi # No <type>
                index.html # No <engine>
                index.html.foo # Non-existent <engine>
            lorem/
                index.txt.jinja2

        >>> site.find_template(u'/')
        (u'index.html.genshi', u'html', u'genshi')
        >>> site.find_template(u'/nonexistent')
        
        >>> site.find_template(u'/hello/')
        (u'hello.html.jinja2', u'html', u'jinja2')
        >>> site.find_template(u'/hello/world')
        
        >>> site.find_template(u'/lorem/')
        (u'lorem/index.txt.jinja2', u'txt', u'jinja2')
        >>> site.find_template(u'/lorem/ipsum')
        """
        path_parts = [part for part in path.split(u'/')
                      if part and part != u'..']
        
        # Regular expression for .<type>.<engine> 
        # where <engine> is a koral engine name
        suffix_re = ur'\.(.+)\.(' + u'|'.join(re.escape(e) for e in
                                              self.koral_site.engines) + u')$'

        searches = [(path_parts, u'index' + suffix_re)]
        # if path_parts is empty (ie. path is u'' or u'/')
        # there is no path_parts[-1]
        if path_parts:
            searches.append((path_parts[:-1],
                             re.escape(path_parts[-1]) + suffix_re))

        for dir_parts, basename_re in searches:
            dirname = os.path.join(self.site_root, *dir_parts)
            if os.path.isdir(dirname):
                for name in os.listdir(dirname):
                    match = re.match(basename_re, name)
                    if match:
                        template = u'/'.join(dir_parts + [name])
                        return template, match.group(1), match.group(2)
